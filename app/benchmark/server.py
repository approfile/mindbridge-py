"""Benchmark child only: isolated namespaces, fixed-order baseline, labelled faults.

Does not add a production route, setting or switch. Imported only by the runner.
"""
from contextvars import ContextVar
import json
import os
import re
from pathlib import Path

_CASE = ContextVar('benchmark_case', default=None)


def record_database_error(error, stage):
    context = _CASE.get()
    if context is None: return
    args = getattr(getattr(error,'orig',None),'args',())
    errno = args[0] if args and type(args[0]) is int else None
    context['diagnostics'].append({'stage':stage,'errorType':type(error).__name__,'driverErrorCode':errno})
    marker = context.get('sampleTraceId','')
    if re.fullmatch(r'[a-f0-9]{32}',marker):
        path = Path(os.environ['BENCHMARK_OUTPUT'])/'private/diagnostics'
        path.mkdir(exist_ok=True)
        (path/(marker+'.json')).write_text(json.dumps(context['diagnostics']),encoding='utf-8')


def add_runtime_route(app):
    from fastapi.routing import APIRoute
    def runtime_state():
        return app.state.turn_runtime.stats()
    # The production app's root StaticFiles mount is a catch-all. Insert this
    # private child-only probe before that mount rather than appending a route.
    app.router.routes.insert(0, APIRoute('/__benchmark/runtime',runtime_state,methods=['GET']))


def create_benchmark_app():
    from app.core.config import get_settings
    from sqlalchemy.engine import make_url
    settings = get_settings()
    database = make_url(settings.database_url).database or ''
    namespace = os.environ['BENCHMARK_NAMESPACE']
    if not re.fullmatch(r'mindbridge_bench_[a-zA-Z0-9_]+', database) or not namespace.startswith('mb-bench:'):
        raise ValueError('Benchmark server refuses non-isolated storage')
    from app.benchmark.protocol import load_dataset
    dataset, _ = load_dataset()
    cases = {row['id']: row for row in dataset['nominal'] + dataset['controlled']}
    from app.services.memory import RedisShortTermMemoryStore
    RedisShortTermMemoryStore._key = lambda self, public_id: namespace + ':' + public_id
    # This namespace is newly generated. Never scan/migrate the business Redis.
    RedisShortTermMemoryStore.upgrade_legacy_cache = lambda self: {'available': self.client is not None, 'lists': 0}
    from app.services.knowledge import KnowledgeService
    retrieve = KnowledgeService.retrieve
    def retrieval(self, *args, **kwargs):
        context = _CASE.get()
        if context and cases.get(context['id'], {}).get('emptyRetrieval'):
            context['emptyRetrievalReached'] = True
            from app.services.observability import span
            with span('benchmark.empty_retrieval', reason_code='CONTROLLED_EMPTY_CONTEXT'):
                pass
            return []
        return retrieve(self, *args, **kwargs)
    KnowledgeService.retrieve = retrieval
    import httpx
    send = httpx.AsyncClient.send
    async def controlled_send(client, request, *args, **kwargs):
        context = _CASE.get()
        case = cases.get(context['id'], {}) if context else {}
        if case.get('fault') and request.url.path.endswith('/chat/completions'):
            payload = json.loads(request.content)
            if payload.get('stream'):
                context['streamAttempts'] += 1
                if case['fault'] == 'all_stream_503' or context['streamAttempts'] == 1:
                    return httpx.Response(503, json={'error': {'type': 'benchmark_injected'}}, request=request,
                                          headers={'x-benchmark-injected': case['fault']})
        return await send(client, request, *args, **kwargs)
    httpx.AsyncClient.send = controlled_send
    from app.services.model_gateway import ModelAttempt
    response = ModelAttempt.response
    def recorded_response(attempt, result):
        response(attempt, result)
        if result.headers.get('x-benchmark-injected'):
            attempt.row['injectedFault'] = result.headers['x-benchmark-injected']
    ModelAttempt.response = recorded_response
    from app.services.chat import ChatService
    from app.services.session_memory import SessionMemoryService
    from sqlalchemy.exc import OperationalError
    synchronize = SessionMemoryService.synchronize
    def diagnosed_synchronize(self, *args, **kwargs):
        try:
            return synchronize(self,*args,**kwargs)
        except OperationalError as error:
            record_database_error(error,'memory.synchronize')
            raise
    SessionMemoryService.synchronize = diagnosed_synchronize
    if os.environ['BENCHMARK_VARIANT'] == 'sequential':
        from app.evaluation.sequential import SequentialWorkflowBaseline
        initialize = ChatService.__init__
        def sequential(self, db, settings, runtime_factory=None):
            initialize(self, db, settings, runtime_factory=SequentialWorkflowBaseline)
        ChatService.__init__ = sequential
    from app.main import create_app
    app = create_app()
    class ContextMiddleware:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            headers = dict(scope.get('headers', []))
            name = headers.get(b'x-benchmark-case', b'').decode('ascii', errors='ignore')
            context = {'id': name, 'streamAttempts': 0, 'emptyRetrievalReached': False,
                       'sampleTraceId':headers.get(b'x-benchmark-sample',b'').decode('ascii',errors='ignore'),'diagnostics':[]}
            token = _CASE.set(context)
            try:
                await self.app(scope, receive, send)
            finally:
                _CASE.reset(token)
    app.add_middleware(ContextMiddleware)
    add_runtime_route(app)
    return app
