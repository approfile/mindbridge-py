import asyncio
import io
import json
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.entities import AgentRunTrace, ToolAuditRecord, ToolJob
from app.schemas.dtos import ChatRequest
from app.services.ai import AiClient
from app.services.chat import ChatService
from app.services.observability import (JsonFormatter, ObservationMiddleware, PrivateRotatingHandler,
    bind_ids, correlation, current_observation, log_event, observation_scope, read_correlation, span)
from app.services.query_rewrite import rewrite_query
from app.services.report import ReportService
from app.services.tool_queue import ToolQueueService
from app.services.trace import AgentTraceService
from test_tool_reliability import ToolRuntimeFixture


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(); self.rows = []
    def emit(self, record):
        self.rows.append(json.loads(JsonFormatter().format(record)))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.capture = Capture(); logger = logging.getLogger('app.observability')
        old = logger.level; logger.setLevel(logging.INFO); logger.addHandler(self.capture)
        self.addCleanup(logger.setLevel, old); self.addCleanup(logger.removeHandler, self.capture)

    def test_json_uses_allowlisted_metadata_and_never_exception_text(self):
        with observation_scope(ensure_run=True):
            try:
                with span('query.rewrite', query='PRIVATE_SENTINEL', authorization='Bearer PRIVATE_SENTINEL'):
                    raise TimeoutError('PRIVATE_SENTINEL')
            except TimeoutError:
                pass
        raw = json.dumps(self.capture.rows)
        self.assertNotIn('PRIVATE_SENTINEL', raw)
        row = self.capture.rows[-1]
        self.assertEqual(row['error_type'], 'TimeoutError')
        self.assertEqual(row['status'], 'error')
        self.assertTrue(row['request_id']); self.assertTrue(row['run_id'])

    def test_async_requests_keep_ids_and_parent_spans_separate(self):
        async def request():
            with observation_scope(ensure_run=True, reuse=False) as state:
                request_id = correlation()['request_id']
                with span('parent'):
                    await asyncio.sleep(0)
                    with span('child'):
                        await asyncio.sleep(0)
                    self.assertEqual(correlation()['request_id'], request_id)
                return state.snapshot()
        async def run():
            return await asyncio.gather(request(), request())
        first, second = asyncio.run(run())
        self.assertNotEqual(first['correlation']['request_id'], second['correlation']['request_id'])
        for snapshot in [first, second]:
            parent = next(s for s in snapshot['spans'] if s['stage'] == 'parent')
            child = next(s for s in snapshot['spans'] if s['stage'] == 'child')
            self.assertEqual(child['parentSpanId'], parent['spanId'])
            self.assertGreaterEqual(parent['durationMs'] + .001, child['durationMs'])
            self.assertLessEqual(parent['exclusiveMs'], parent['durationMs'])
        self.assertIsNone(current_observation())

    def test_durable_carrier_rejects_untrusted_or_malformed_fields(self):
        self.assertEqual(read_correlation('not json'), {})
        clean = read_correlation({'request_id': 'line\ninjection', 'run_id': 'safe-run',
                                  'trace_id': True, 'job_id': 4, 'prompt': 'secret'})
        self.assertEqual(clean, {'run_id': 'safe-run', 'job_id': 4})

    def test_span_cap_reports_loss_instead_of_unbounded_growth(self):
        with observation_scope(ensure_run=True) as state:
            for _ in range(515):
                with span('small'): pass
            snapshot = state.snapshot()
        self.assertEqual(len(snapshot['spans']), 512)
        self.assertEqual(snapshot['droppedSpans'], 3)

    def test_rotating_files_are_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'events.jsonl'
            handler = PrivateRotatingHandler(path, maxBytes=1024, backupCount=2, encoding='utf-8')
            handler.setFormatter(JsonFormatter())
            try:
                for _ in range(80):
                    record = logging.LogRecord('app.test', logging.INFO, '', 0, 'fixed message', (), None)
                    handler.emit(record)
            finally:
                handler.close()
            files = list(Path(temporary).iterdir())
            self.assertEqual(len(files), 3)
            for file in files:
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                for line in file.read_text().splitlines(): json.loads(line)


class QueryRewriteTests(unittest.TestCase):
    def test_transport_timeout_keeps_failed_model_call_and_degraded_rewrite(self):
        client = AiClient(Settings(_env_file=None, ai_provider='openai', openai_api_key='test',
                                   openai_base_url='https://example.test'))
        with observation_scope(ensure_run=True) as state, patch('app.services.ai.httpx.post',
                side_effect=httpx.ReadTimeout('PRIVATE_TRANSPORT_ERROR')):
            result = rewrite_query(client, [], '睡前很焦虑')
            snapshot = state.snapshot()
        self.assertEqual(result.reasonCode, 'REWRITE_TIMEOUT')
        self.assertEqual(snapshot['logicalModelCalls'], 1)
        call = next(s for s in snapshot['spans'] if s['stage'] == 'model.complete')
        self.assertEqual(call['status'], 'error')
        self.assertEqual(call['attributes']['error_type'], 'ReadTimeout')

    def test_empty_invalid_timeout_and_call_failure_are_distinct_degradations(self):
        for value, code in [(' ', 'REWRITE_EMPTY_OUTPUT'), (None, 'REWRITE_INVALID_OUTPUT'),
                            (httpx.ReadTimeout('private'), 'REWRITE_TIMEOUT'),
                            (RuntimeError('private'), 'REWRITE_CALL_FAILED')]:
            with self.subTest(code=code), observation_scope(ensure_run=True) as state:
                client = Mock()
                if isinstance(value, Exception): client.complete.side_effect = value
                else: client.complete.return_value = value
                result = rewrite_query(client, [], '原始问题' * 30)
                self.assertEqual(result.query, '原始问题' * 30)
                self.assertEqual(result.reasonCode, code); self.assertTrue(result.degraded)
                self.assertFalse(result.truncated)
                self.assertNotIn('query', result.metadata())
                self.assertEqual(state.snapshot()['degradations'][0]['reasonCode'], code)

    def test_success_keeps_full_query_within_declared_budget(self):
        client = Mock(); client.complete.return_value = ' 检索词 ' * 30
        result = rewrite_query(client, [], '原始输入')
        self.assertEqual(result.query, (' 检索词 ' * 30).strip())
        self.assertFalse(result.truncated); self.assertFalse(result.degraded)
        self.assertEqual(result.source, 'model')


class OnlineObservationTests(ToolRuntimeFixture):
    def setUp(self):
        super().setUp()
        self.capture = Capture(); logger = logging.getLogger('app.observability')
        old = logger.level; logger.setLevel(logging.INFO); logger.addHandler(self.capture)
        self.addCleanup(logger.setLevel, old); self.addCleanup(logger.removeHandler, self.capture)

    def client(self):
        app = FastAPI(); app.add_middleware(ObservationMiddleware)
        @app.post('/stream')
        async def route(request: ChatRequest):
            return StreamingResponse(ChatService(self.db, self.settings).stream_chat(self.user, request),
                                     media_type='text/event-stream')
        return TestClient(app)

    def stream(self, text='最近考试压力大，连续几天失眠，想聊聊。'):
        with self.client() as client:
            response = client.post('/stream', json={'message': text}, headers={'X-Request-ID': 'caller-collision'})
        self.assertEqual(response.status_code, 200)
        self.db.expire_all()
        trace = self.db.query(AgentRunTrace).order_by(AgentRunTrace.id.desc()).first()
        dto = next(t for t in ReportService(self.db,sensitive=True).agent_run_traces() if t.id == trace.id)
        return response, trace, dto

    def test_header_sse_trace_and_online_spans_link_to_same_run(self):
        response, trace, dto = self.stream()
        events = self.events([response.text]); meta = next(e for e in events if e['type'] == 'meta')
        self.assertTrue(any(e['type'] == 'done' for e in events))
        self.assertNotEqual(response.headers['X-Request-ID'], 'caller-collision')
        self.assertEqual(dto.requestId, response.headers['X-Request-ID'])
        self.assertEqual(meta['requestId'], dto.requestId)
        observation = dto.observability
        self.assertEqual(sum(e.get('kind') == 'observability' for e in dto.agentSteps), 1)
        self.assertEqual(observation['phase'], 'complete')
        self.assertEqual(observation['correlation']['trace_id'], trace.id)
        stages = observation['stageTotalsMs']
        for name in ['turn.total', 'planning.total', 'memory.load', 'query.rewrite', 'retrieval.total',
                     'reply.generate', 'reply.rules', 'reply.review', 'persistence.assistant', 'sse.emit']:
            self.assertIn(name, stages)
        self.assertIsNotNone(observation['serverFirstTokenMs'])
        self.assertEqual(observation['streamStatus'], 'accepted')
        rows = [r for r in self.capture.rows if r['request_id'] == dto.requestId]
        self.assertTrue(rows)
        self.assertEqual({r['run_id'] for r in rows if r['run_id']}, {dto.runId})
        reviewed = max(i for i, r in enumerate(rows) if r.get('stage') == 'reply.review')
        first_token = next(i for i, r in enumerate(rows) if r['event'] == 'http.first_token')
        self.assertGreater(first_token, reviewed)

    def test_context_artifact_and_completed_trace_expose_rewrite_timeout(self):
        original = AiClient.complete
        def complete(ai, messages, **kwargs):
            if any('把学生输入改写' in m.content for m in messages):
                raise httpx.ReadTimeout('PRIVATE_REWRITE_FAILURE')
            return original(ai, messages, **kwargs)
        with patch.object(AiClient, 'complete', complete):
            response, trace, dto = self.stream()
        context = next(e['payload'] for e in dto.agentSteps if e.get('artifactKind') == 'context')
        self.assertEqual(context['queryRewrite']['reasonCode'], 'REWRITE_TIMEOUT')
        self.assertEqual(context['queryRewrite']['source'], 'original_input')
        self.assertEqual(dto.observability['degradations'][0]['reasonCode'], 'REWRITE_TIMEOUT')
        self.assertNotIn('PRIVATE_REWRITE_FAILURE', json.dumps(self.capture.rows))
        self.assertIn('event: done', response.text)

    def test_repeated_final_snapshot_keeps_business_events_and_only_one_snapshot(self):
        _, trace, dto = self.stream()
        previous = [e for e in dto.agentSteps if e.get('kind') != 'observability']
        with observation_scope(ids=dto.observability['correlation'], reuse=False) as state:
            AgentTraceService(self.db).append_observation(trace.id, state)
            AgentTraceService(self.db).append_observation(trace.id, state)
        final = json.loads(self.db.get(AgentRunTrace, trace.id).agent_steps_json)
        self.assertEqual(sum(e.get('kind') == 'observability' for e in final), 1)
        self.assertEqual([e for e in final if e.get('kind') != 'observability'], previous)

    def test_worker_restores_origin_ids_and_dedup_does_not_relabel_old_job(self):
        _, trace, dto = self.stream()
        jobs = self.db.query(ToolJob).filter_by(report_id=trace.report_id).all()
        carrier = jobs[0].correlation_json
        with observation_scope(ensure_run=True, reuse=False):
            ToolQueueService(self.db, self.settings).enqueue_report(trace.report_id)
        self.db.expire_all(); self.assertEqual(self.db.get(ToolJob, jobs[0].id).correlation_json, carrier)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(self.worker._run_job, jobs[0].id).result()
        audit = self.rows(ToolAuditRecord)[0]
        ids = json.loads(audit.payload)['correlation']
        self.assertEqual(ids['request_id'], dto.requestId)
        self.assertEqual(ids['run_id'], dto.runId)
        self.assertEqual(ids['job_id'], jobs[0].id)
        self.assertTrue(any(r['event'] == 'tool.job.finished' and r['run_id'] == dto.runId for r in self.capture.rows))
        self.assertIsNone(current_observation())

    def test_real_mcp_stdio_restores_durable_carrier_without_polluting_protocol(self):
        self.settings.tool_queue_enabled = False
        _, trace, dto = self.stream()
        job = self.db.query(ToolJob).filter_by(report_id=trace.report_id).one()
        self.worker._run_job(job.id)
        self.db.expire_all(); self.assertEqual(self.db.get(ToolJob, job.id).status, 'SUCCESS')
        audit = self.rows(ToolAuditRecord)[0]
        self.assertEqual(json.loads(audit.payload)['source'], 'mcp-stdio')
        self.assertEqual(json.loads(audit.payload)['correlation']['run_id'], dto.runId)

    def test_cancelled_stream_retains_trace_and_pending_outbox(self):
        async def run():
            stream = ChatService(self.db, self.settings).stream_chat(self.user, ChatRequest(message='我想伤害自己。'))
            await anext(stream)
            await stream.aclose()
        asyncio.run(run()); self.db.expire_all()
        dto = ReportService(self.db).agent_run_traces()[0]
        self.assertEqual(dto.observability['streamStatus'], 'cancelled')
        self.assertEqual(dto.observability['phase'], 'complete')
        self.assertIn('turn.total', dto.observability['stageTotalsMs'])
        jobs = self.db.query(ToolJob).filter_by(report_id=dto.reportId).all()
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(j.status == 'PENDING' for j in jobs))

    def test_failed_generation_has_error_span_and_safe_fallback_not_http_success_claim(self):
        async def broken(ai, messages):
            raise RuntimeError('PRIVATE_GENERATION_ERROR')
            yield 'never'
        with patch.object(AiClient, 'stream', broken):
            response, _, dto = self.stream()
        self.assertIn('event: error', response.text); self.assertNotIn('event: done', response.text)
        self.assertEqual(dto.observability['streamStatus'], 'safe_fallback')
        generation = next(s for s in dto.observability['spans'] if s['stage'] == 'reply.generate')
        self.assertEqual(generation['status'], 'error')
        completed = next(r for r in self.capture.rows if r['event'] == 'http.completed')
        self.assertEqual(completed['http_status'], 200)
        self.assertEqual(completed['stream_status'], 'safe_fallback')
