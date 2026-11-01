"""Local structured tracing. No prompts, replies, credentials or exception text.

ContextVars isolate concurrent requests; a request-owned collector is shared by
its ASGI streaming task. Durable jobs carry IDs, never the mutable collector.
This is process-local instrumentation, not an OpenTelemetry implementation.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from contextlib import aclosing
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from app.services.turn_budget import checkpoint

_CURRENT = ContextVar('mindbridge_observation', default=None)
_PARENT = ContextVar('mindbridge_span', default=None)
IDS = ('request_id', 'run_id', 'session_id', 'trace_id', 'report_id', 'job_id')
_ATTRS = {'agent', 'provider', 'model', 'attempt', 'tool', 'status', 'reason_code', 'error_type',
          'result_count', 'input_chars', 'output_chars', 'truncated', 'mode', 'method', 'route',
          'http_status', 'stream_status', 'duration_ms', 'first_chunk_ms', 'job_age_ms',
          'eligible_wait_ms', 'stage', 'span_id', 'parent_span_id', 'server_first_token_ms',
          'first_body_ms', 'action', 'budget_seconds', 'active_turns', 'remaining_ms'}
_LOG = logging.getLogger('app.observability')


def _safe_attributes(values):
    return {k: v[:160] if isinstance(v, str) else v for k, v in values.items()
            if k in _ATTRS and (v is None or isinstance(v, (str, int, float, bool)))}


def current_observation():
    return _CURRENT.get()


def correlation():
    state = _CURRENT.get()
    return dict(state.ids) if state else {k: None for k in IDS}


def read_correlation(raw):
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            return {}
        clean = {}
        for key in IDS:
            value = data.get(key)
            if key in {'trace_id', 'report_id', 'job_id'}:
                if type(value) is int and value > 0:
                    clean[key] = value
            elif isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value):
                clean[key] = value
        return clean
    except (ValueError, TypeError):
        return {}


def bind_ids(**values):
    state = _CURRENT.get()
    if state:
        with state.lock:
            state.ids.update(read_correlation(values))


def log_event(event, *, level=logging.INFO, **values):
    _LOG.log(level, event, extra={'event': event, 'correlation': correlation(),
                                'attributes': _safe_attributes(values)})


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Resolve at emission, not formatting: QueueHandlers may format later.
        ids = getattr(record, 'correlation', {key: None for key in IDS})
        result = {'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                  'level': record.levelname, 'logger': record.name,
                  'event': getattr(record, 'event', 'application.log'), **ids}
        result.update(_safe_attributes(getattr(record, 'attributes', {})))
        # Never format arbitrary %-arguments or exception messages. Existing
        # app log sites use fixed templates; the template alone is useful.
        if not hasattr(record, 'event'):
            result['message'] = str(record.msg)[:240]
        if record.exc_info and record.exc_info[0]:
            result['error_type'] = record.exc_info[0].__name__
        return json.dumps(result, ensure_ascii=False, allow_nan=False)


class CorrelationFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'correlation'):
            record.correlation = correlation()
        return True


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def configure_logging(settings):
    logger = logging.getLogger('app')
    for handler in list(logger.handlers):
        logger.removeHandler(handler); handler.close()
    handlers = [logging.StreamHandler(sys.stderr)]  # stdout remains safe for MCP JSON-RPC
    if settings.observability_log_file:
        path = Path(settings.observability_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(PrivateRotatingHandler(str(path), maxBytes=settings.observability_log_max_bytes,
                                              backupCount=settings.observability_log_backups, encoding='utf-8'))
    for handler in handlers:
        handler.setFormatter(JsonFormatter()); handler.addFilter(CorrelationFilter()); logger.addHandler(handler)
    logger.setLevel(logging.INFO); logger.propagate = False


class Observation:
    def __init__(self, ids=None):
        self.ids = {key: None for key in IDS}; self.ids.update(read_correlation(ids))
        self.started_ns = time.perf_counter_ns()
        self.lock = threading.RLock()
        self.spans = []
        self.degradations = []
        self.dropped_spans = 0
        self.server_first_token_ms = None
        self.stream_status = 'not_started'
        self.usage_calls = {}
        self.usage_attempts = []
        self.usage_cache_events = []
        self.usage_ledger_errors = 0
        self.usage_session_factory = None
        self.usage_scope = 'unbound'
        self.usage_user_id = None
        self.usage_db_lock = threading.RLock()
        self.usage_finalized = False
        self.usage_deferred = False

    def elapsed_ms(self):
        return (time.perf_counter_ns() - self.started_ns) / 1_000_000

    def snapshot(self, phase='complete'):
        from app.services.model_gateway import usage_snapshot
        usage = usage_snapshot(self)
        with self.lock:
            rows = [dict(row) for row in self.spans]
            for row in rows:
                intervals = sorted((max(row['startMs'], child['startMs']), min(row['endMs'], child['endMs']))
                                   for child in rows if child['parentSpanId'] == row['spanId'])
                covered = 0.; end = row['startMs']
                for left, right in intervals:
                    covered += max(0., right - max(left, end)); end = max(end, right)
                row['exclusiveMs'] = round(max(0., row['durationMs'] - covered), 3)
            totals = Counter()
            for row in rows:
                totals[row['stage']] += row['durationMs']
            return {'schemaVersion': 1, 'phase': phase, 'correlation': dict(self.ids), 'spans': rows,
                    'stageTotalsMs': {key: round(value, 3) for key, value in totals.items()},
                    'durationSemantics': 'Monotonic inclusive spans; do not sum parents and children. exclusiveMs subtracts the union of direct children.',
                    'serverFirstTokenMs': self.server_first_token_ms, 'streamStatus': self.stream_status,
                    'logicalModelCalls': sum(r['stage'] in {'model.complete', 'model.stream', 'model.review'} for r in rows),
                    'degradations': list(self.degradations), 'droppedSpans': self.dropped_spans,
                    'tokenUsage': usage['tokens'], 'currencyCost': usage['cost'] if usage['httpAttempts'] else None,
                    'modelUsage': usage, 'modelProfile': getattr(self, 'model_profile', None)}


@contextmanager
def observation_scope(*, ids=None, reuse=True, ensure_run=False):
    existing = _CURRENT.get()
    if existing is not None and reuse:
        state = existing; token = None; parent = None
    else:
        state = Observation(ids); token = _CURRENT.set(state); parent = _PARENT.set(None)
    if ensure_run:
        if not state.ids['request_id']:
            state.ids['request_id'] = uuid.uuid4().hex
        if not state.ids['run_id']:
            state.ids['run_id'] = uuid.uuid4().hex
    try:
        yield state
    finally:
        if token is not None:
            _PARENT.reset(parent); _CURRENT.reset(token)


class Span:
    def __init__(self, stage, attributes):
        self.stage = stage; self.attributes = _safe_attributes(attributes)
        self.status = 'ok'; self.span_id = uuid.uuid4().hex[:16]

    def set(self, **values):
        self.attributes.update(_safe_attributes(values))
        self.status = values.get('status', self.status)


@contextmanager
def span(stage, **attributes):
    from app.services.model_gateway import stage_scope
    node = Span(stage, attributes); state = _CURRENT.get()
    parent_id = _PARENT.get(); token = _PARENT.set(node.span_id)
    started = time.perf_counter_ns()
    stage_context = stage_scope(stage)
    stage_context.__enter__()
    try:
        yield node
    except BaseException as exc:
        node.status = 'cancelled' if type(exc).__name__ in {'CancelledError', 'GeneratorExit'} else 'error'
        node.attributes['error_type'] = type(exc).__name__
        raise
    finally:
        ended = time.perf_counter_ns(); _PARENT.reset(token)
        stage_context.__exit__(None, None, None)
        duration = (ended - started) / 1_000_000
        if state is not None:
            with state.lock:
                if len(state.spans) < 512:
                    state.spans.append({'spanId': node.span_id, 'parentSpanId': parent_id, 'stage': stage,
                                        'startMs': (started - state.started_ns) / 1_000_000,
                                        'endMs': (ended - state.started_ns) / 1_000_000,
                                        'durationMs': round(duration, 3), 'status': node.status,
                                        'attributes': dict(node.attributes)})
                else:
                    state.dropped_spans += 1
        log_event('span.finished', stage=stage, span_id=node.span_id, parent_span_id=parent_id,
                  duration_ms=round(duration, 3), **{**node.attributes, 'status': node.status})


def record_degradation(stage, reason_code, *, error_type=None):
    state = _CURRENT.get()
    if state is not None:
        with state.lock:
            state.degradations.append({'stage': stage, 'reasonCode': reason_code, 'errorType': error_type})
    log_event('operation.degraded', level=logging.WARNING, stage=stage,
              reason_code=reason_code, error_type=error_type, status='degraded')


def timed(stage):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with span(stage) as node:
                result = function(*args, **kwargs)
                if isinstance(result, list):
                    node.set(result_count=len(result))
                return result
        return wrapped
    return decorate


def observed_run(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with observation_scope(ensure_run=True):
            return function(*args, **kwargs)
    return wrapped


def model_operation(stage):
    from app.services.model_gateway import model_call
    def decorate(function):
        def attributes(client):
            settings = client.settings; provider = settings.ai_provider.lower()
            return dict(agent=getattr(client, 'agent_name', None), provider=provider,
                        model=settings.openai_model if provider == 'openai' else settings.ollama_model if provider == 'ollama' else 'mock')
        if inspect.isasyncgenfunction(function):
            @functools.wraps(function)
            async def stream(client, *args, **kwargs):
                start = time.perf_counter(); first = True
                with model_call(client, stage), span(stage, **attributes(client)) as node:
                    checkpoint()
                    async with aclosing(function(client, *args, **kwargs)) as source:
                        async for value in source:
                            checkpoint()
                            if first and value:
                                node.set(first_chunk_ms=round((time.perf_counter() - start) * 1000, 3)); first = False
                            yield value
            return stream
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(client, *args, **kwargs):
                with model_call(client, stage), span(stage, **attributes(client)):
                    checkpoint()
                    try:
                        value = await function(client, *args, **kwargs)
                    except Exception:
                        checkpoint()
                        raise
                    checkpoint()
                    return value
            return asynchronous
        @functools.wraps(function)
        def synchronous(client, *args, **kwargs):
            with model_call(client, stage), span(stage, **attributes(client)):
                checkpoint()
                try:
                    value = function(client, *args, **kwargs)
                except Exception:
                    checkpoint()
                    raise
                checkpoint()
                return value
        return synchronous
    return decorate


class ObservationMiddleware:
    """Pure ASGI wrapper: request timing includes the streaming body lifecycle."""
    def __init__(self, app, session_factory=None):
        self.app = app
        self.session_factory = session_factory

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        # Always server-generated; a caller cannot merge two independent runs.
        with observation_scope(ids={'request_id': uuid.uuid4().hex}, reuse=False) as state:
            from app.services.model_gateway import bind_ledger, persist_run
            bind_ledger(state, self.session_factory,
                        scope='online_chat' if scope.get('path') == '/api/chat/stream' else 'http_model_operation')
            status = None; first_body = None; failure = None
            async def observed_send(message):
                nonlocal status, first_body
                if message['type'] == 'http.response.start':
                    status = message['status']
                    message = dict(message)
                    message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'x-request-id']
                    message['headers'].append((b'x-request-id', state.ids['request_id'].encode()))
                if message['type'] == 'http.response.body' and message.get('body'):
                    if first_body is None:
                        first_body = state.elapsed_ms()
                    if state.server_first_token_ms is None and message['body'].startswith(b'event: token\n'):
                        state.server_first_token_ms = round(state.elapsed_ms(), 3)
                        log_event('http.first_token', server_first_token_ms=state.server_first_token_ms)
                await send(message)
            try:
                await self.app(scope, receive, observed_send)
            except BaseException as exc:
                failure = type(exc).__name__
                raise
            finally:
                if not state.usage_deferred:
                    persist_run(state, finalized=True)
                route = getattr(scope.get('route'), 'path', 'unmatched')
                log_event('http.completed', method=scope.get('method'), route=route,
                          http_status=status, duration_ms=round(state.elapsed_ms(), 3),
                          first_body_ms=round(first_body, 3) if first_body is not None else None,
                          server_first_token_ms=state.server_first_token_ms,
                          stream_status=state.stream_status, error_type=failure)
