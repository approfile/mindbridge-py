import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from datetime import datetime, timezone
import json
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.core.config import Settings
from app.core.database import get_db
from app.core.security import hash_password
from app.models.entities import DataAccessAudit, KnowledgeChunk, ModelUsageAttempt, ModelUsageRun, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.knowledge import KnowledgeService
from app.services.memory import RedisShortTermMemoryStore
from app.services.model_gateway import (ModelAttempt, aggregate_usage, bind_ledger, estimate_cost,
    normalize_usage, persist_run, record_cache, stage_scope, usage_snapshot)
from app.services.model_retry import retry_transport
from app.services.observability import ObservationMiddleware, observation_scope, span
from app.services.session_memory import SessionMemoryService
from app.services.usage_reporting import attempts_csv, list_usage_runs, usage_report, usage_run_detail
from app.services.vector_store import ChromaKnowledgeStore
from test_tool_reliability import ToolRuntimeFixture


USAGE = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
         'prompt_cache_hit_tokens': 80, 'prompt_cache_miss_tokens': 20,
         'completion_tokens_details': {'reasoning_tokens': 5}}
MESSAGES = [AiMessage(role='user', content='SYNTHETIC_PRIVATE_SENTINEL')]


def settings(**values):
    return Settings(_env_file=None, ai_provider='openai', openai_api_key='SECRET_KEY_SENTINEL',
                    openai_base_url='https://api.deepseek.com', openai_model='deepseek-flash', **values)


def completion(usage=USAGE, **values):
    return {'id': 'test-request', 'model': 'DeepSeek-V4.1-Flash-1017',
            'choices': [{'message': {'content': 'test reply'}, 'finish_reason': 'stop'}], 'usage': usage, **values}


def response(data, status=200):
    return httpx.Response(status, json=data, request=httpx.Request('POST', 'https://example.test'))


def stream_transport(chunks, handler=None):
    def respond(request):
        if handler:
            handler(json.loads(request.content))
        lines = [chunk if isinstance(chunk, str) else json.dumps(chunk) for chunk in chunks]
        return httpx.Response(200, text=''.join('data: ' + line + '\n\n' for line in lines))
    original = httpx.AsyncClient
    return patch('app.services.ai.httpx.AsyncClient', side_effect=lambda **kw: original(transport=httpx.MockTransport(respond)))


class UsageSemanticsTests(unittest.TestCase):
    def test_provider_cache_reasoning_and_total_are_not_double_counted(self):
        row = normalize_usage({'usage': USAGE})
        self.assertEqual((row['inputTokens'], row['outputTokens'], row['totalTokens']), (100, 20, 120))
        self.assertEqual((row['cacheHitTokens'], row['cacheMissTokens'], row['reasoningTokens']), (80, 20, 5))
        self.assertEqual(row['usageStatus'], 'reported')

    def test_nested_openai_cached_details_and_derived_total(self):
        row = normalize_usage({'usage': {'input_tokens': 10, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 4}}})
        self.assertEqual((row['cacheHitTokens'], row['cacheMissTokens'], row['totalTokens']), (4, 6, 12))

    def test_missing_is_unknown_and_explicit_zero_is_real_zero(self):
        self.assertEqual(normalize_usage({})['usageStatus'], 'missing')
        self.assertIsNone(normalize_usage({})['totalTokens'])
        row = normalize_usage({'usage': {'prompt_tokens': 0, 'completion_tokens': 0}})
        self.assertEqual(row['usageStatus'], 'reported'); self.assertEqual(row['totalTokens'], 0)
        self.assertIsNone(row['cacheHitTokens'])

    def test_invalid_counts_are_excluded_from_coverage(self):
        for update in [{'prompt_tokens': -1}, {'completion_tokens': True}, {'total_tokens': 121},
                       {'total_tokens': -1}, {'total_tokens': True}, {'prompt_cache_hit_tokens': False},
                       {'completion_tokens_details': {'reasoning_tokens': '5'}},
                       {'prompt_cache_hit_tokens': 110}, {'prompt_cache_miss_tokens': 21},
                       {'completion_tokens_details': {'reasoning_tokens': 21}}]:
            with self.subTest(update=update):
                row = normalize_usage({'usage': {**USAGE, **update}})
                self.assertEqual(row['usageStatus'], 'invalid')

    def test_embedding_and_ollama_final_counts(self):
        row = normalize_usage({'usage': {'prompt_tokens': 40, 'total_tokens': 40}}, operation='embedding')
        self.assertEqual((row['inputTokens'], row['outputTokens'], row['totalTokens']), (40, 0, 40))
        self.assertEqual(normalize_usage({'done': False}, 'ollama')['usageStatus'], 'missing')
        row = normalize_usage({'done': True, 'prompt_eval_count': 30, 'eval_count': 4}, 'ollama')
        self.assertEqual(row['totalTokens'], 34)

    def priced_row(self, **values):
        return {**normalize_usage({'usage': USAGE}), 'provider': 'openai', 'host': 'api.deepseek.com',
                'operation': 'chat', 'requestedModel': 'deepseek-flash', 'actualModel': 'DeepSeek-V4.1-Flash-1017',
                'startedAt': '2026-11-01T01:00:00+00:00', **values}

    def test_decimal_price_uses_real_cache_split_and_version(self):
        cost = estimate_cost(self.priced_row(), settings())
        self.assertEqual(cost['amount'], '0.00001524')
        self.assertEqual(cost['currency'], 'USD')
        self.assertTrue(cost['priceVersion']); self.assertTrue(cost['priceSource'].startswith('https://'))

    def test_missing_split_model_mismatch_and_expired_price_stay_unknown(self):
        for changes, expected in [({'cacheHitTokens': None}, 'cache_split_unknown'),
                                  ({'actualModel': 'unpriced-model'}, 'price_not_configured'),
                                  ({'host': 'proxy.example.test'}, 'price_not_configured'),
                                  ({'startedAt': '2027-01-01T00:00:00+00:00'}, 'price_expired_or_not_effective'),
                                  ({'usageStatus': 'missing'}, 'usage_unknown'),
                                  ({'provider': 'ollama'}, 'infrastructure_cost_not_configured')]:
            with self.subTest(changes=changes):
                cost = estimate_cost(self.priced_row(**changes), settings())
                self.assertEqual(cost['status'], expected); self.assertIsNone(cost['amount'])

    def test_unverified_holiday_calendar_does_not_guess_peak_tariff(self):
        cost = estimate_cost(self.priced_row(startedAt='2026-11-02T02:00:00+00:00'), settings())
        self.assertEqual(cost['status'], 'holiday_tier_unverified'); self.assertIsNone(cost['amount'])

    def test_explicitly_verified_peak_and_holiday_dates_choose_correct_rate(self):
        catalog = json.loads((settings().project_root/'app/pricing/model-prices-v1.json').read_text())
        row = self.priced_row(startedAt='2026-11-02T02:00:00+00:00')
        catalog['entries'][0]['verifiedPeakDatesUtc'] = ['2026-11-02']
        with patch('app.services.model_gateway.Path.read_text', return_value=json.dumps(catalog)):
            peak = estimate_cost(row, settings())
        self.assertEqual((peak['tier'], peak['amount']), ('peak', '0.00003048'))
        catalog['entries'][0]['verifiedPeakDatesUtc'] = []
        catalog['entries'][0]['verifiedHolidayDatesUtc'] = ['2026-11-02']
        with patch('app.services.model_gateway.Path.read_text', return_value=json.dumps(catalog)):
            holiday = estimate_cost(row, settings())
        self.assertEqual((holiday['tier'], holiday['amount']), ('off_peak', '0.00001524'))

    def test_embedding_price_and_currencies_remain_separate(self):
        row = self.priced_row(host='dashscope.aliyuncs.com', operation='embedding',
                              requestedModel='qwen3.7-text-embedding', actualModel='qwen3.7-text-embedding', inputTokens=100, outputTokens=0)
        cost = estimate_cost(row, settings())
        self.assertEqual((cost['currency'], cost['amount']), ('CNY', '0.00005'))
        rows = [{**self.priced_row(), 'logicalCallId': str(n), 'status': 'completed', 'cost': value}
                for n, value in enumerate([cost, estimate_cost(self.priced_row(), settings())])]
        self.assertEqual(set(aggregate_usage(rows, [])['cost']['knownAmounts']), {'USD', 'CNY'})

    def test_cache_rates_have_separate_denominators_and_unknown_subset(self):
        rows = [{**self.priced_row(), 'logicalCallId': 'a', 'status': 'completed', 'cost': {}},
                {**self.priced_row(usageStatus='missing'), 'logicalCallId': 'b', 'status': 'failed', 'cost': {}}]
        events = [{'layer': 'redis_session_history', 'hits': 1, 'misses': 1, 'errors': 2,
                   'bypasses': 3, 'durationMs': 2, 'unit': 'requests'}]
        summary = aggregate_usage(rows, events)
        self.assertEqual(summary['usageCoverage'], .5)
        self.assertEqual(summary['providerCacheHitRate'], .8)
        self.assertEqual(summary['cacheLayers']['redis_session_history']['hitRate'], .5)
        self.assertFalse(summary['tokensComplete']); self.assertEqual(summary['tokens']['totalTokens'], 120)
        self.assertIn('query_embedding', summary['unimplementedCaches'])


class HttpAccountingTests(unittest.TestCase):
    def test_sync_usage_and_failure_preserve_accounting_without_private_content(self):
        ai = AiClient(settings()); ai.agent_name = 'UnderstandingAgent'
        with observation_scope(ensure_run=True, reuse=False) as state, span('intent.classify'), patch('app.services.ai.httpx.post', return_value=response(completion())):
            self.assertEqual(ai.complete(MESSAGES), 'test reply')
        row = usage_snapshot(state)['attempts'][0]
        self.assertEqual((row['agent'], row['stage'], row['totalTokens']), ('UnderstandingAgent', 'intent.classify', 120))
        serialized = json.dumps(usage_snapshot(state))
        self.assertNotIn('PRIVATE_SENTINEL', serialized); self.assertNotIn('SECRET_KEY_SENTINEL', serialized)
        self.assertNotIn('test reply', serialized)
        with observation_scope(reuse=False) as state, patch('app.services.ai.httpx.post', return_value=response(completion(choices=[{'finish_reason': 'length'}]))):
            with self.assertRaises(RuntimeError): ai.complete(MESSAGES)
        self.assertEqual(state.usage_attempts[0]['status'], 'failed')
        self.assertEqual(state.usage_attempts[0]['totalTokens'], 120)

    def test_async_review_captures_usage(self):
        original = httpx.AsyncClient
        with observation_scope(reuse=False) as state, patch('app.services.ai.httpx.AsyncClient', side_effect=lambda **kw: original(transport=httpx.MockTransport(lambda request: response(completion())))):
            self.assertEqual(asyncio.run(AiClient(settings()).complete_async(MESSAGES)), 'test reply')
        self.assertEqual(state.usage_attempts[0]['operationStage'], 'model.review')
        self.assertEqual(state.usage_attempts[0]['totalTokens'], 120)

    def stream(self, chunks, handler=None, **config):
        async def run():
            return ''.join([value async for value in AiClient(settings(**config)).stream(MESSAGES)])
        with observation_scope(reuse=False) as state, stream_transport(chunks, handler):
            result = asyncio.run(run())
        return result, usage_snapshot(state)

    def test_stream_usage_only_chunk_and_repeated_cumulative_usage(self):
        chunks = [{'model': 'deepseek-flash', 'choices': [{'delta': {'content': 'hello'}, 'finish_reason': None}]},
                  {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': USAGE},
                  {'choices': [], 'usage': USAGE}, '[DONE]']
        payloads = []
        result, snapshot = self.stream(chunks, payloads.append)
        self.assertEqual(result, 'hello'); self.assertEqual(snapshot['tokens']['totalTokens'], 120)
        self.assertEqual(snapshot['httpAttempts'], 1)
        self.assertEqual(payloads[0]['stream_options'], {'include_usage': True})
        self.assertIsNotNone(snapshot['attempts'][0]['firstTokenMs'])

    def test_usage_in_final_content_chunk_is_captured(self):
        result, snapshot = self.stream([{'model': 'deepseek-flash', 'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': USAGE}, '[DONE]'])
        self.assertEqual(result, 'ok'); self.assertEqual(snapshot['tokens']['totalTokens'], 120)

    def test_missing_stream_usage_is_unknown_and_compatibility_switch_works(self):
        payloads = []
        _, snapshot = self.stream([{'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}, '[DONE]'], payloads.append, model_stream_include_usage=False)
        self.assertIsNone(snapshot['tokens']); self.assertEqual(snapshot['unknownUsageAttempts'], 1)
        self.assertNotIn('stream_options', payloads[0])

    def test_broken_stream_records_failed_unknown_attempt(self):
        async def run():
            return [value async for value in AiClient(settings()).stream(MESSAGES)]
        with observation_scope(reuse=False) as state, stream_transport([{'choices': [{'delta': {'content': 'partial'}}]}]):
            with self.assertRaises(httpx.RemoteProtocolError): asyncio.run(run())
        self.assertEqual(state.usage_attempts[0]['status'], 'failed')
        self.assertEqual(state.usage_attempts[0]['usageStatus'], 'missing')

    def test_truncated_stream_retains_later_usage_only_chunk(self):
        async def run():
            return [value async for value in AiClient(settings()).stream(MESSAGES)]
        chunks = [{'model': 'deepseek-flash', 'choices': [{'delta': {}, 'finish_reason': 'length'}]},
                  {'choices': [], 'usage': USAGE}, '[DONE]']
        with observation_scope(reuse=False) as state, stream_transport(chunks):
            with self.assertRaises(RuntimeError): asyncio.run(run())
        self.assertEqual(state.usage_attempts[0]['status'], 'failed')
        self.assertEqual(state.usage_attempts[0]['totalTokens'], 120)

    def test_stream_cancel_closes_attempt_before_observation_exit(self):
        async def run():
            async with aclosing(AiClient(settings()).stream(MESSAGES)) as source:
                self.assertEqual(await anext(source), 'first')
        with observation_scope(reuse=False) as state, stream_transport([{'choices': [{'delta': {'content': 'first'}}]}, '[DONE]']):
            asyncio.run(run())
            self.assertEqual(state.usage_attempts[0]['status'], 'cancelled')
            self.assertEqual(state.usage_attempts[0]['usageStatus'], 'missing')

    def test_ollama_stream_final_chunk_and_local_cost_unknown(self):
        original = httpx.AsyncClient
        text = '\n'.join(json.dumps(row) for row in [{'model': 'local:latest', 'message': {'content': 'local'}, 'done': False},
                {'model': 'local:latest', 'message': {'content': ''}, 'done': True, 'prompt_eval_count': 30, 'eval_count': 4}])
        config = Settings(_env_file=None, ai_provider='ollama')
        async def run(): return ''.join([value async for value in AiClient(config).stream(MESSAGES)])
        with observation_scope(reuse=False) as state, patch('app.services.ai.httpx.AsyncClient', side_effect=lambda **kw: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=text)))):
            self.assertEqual(asyncio.run(run()), 'local')
        self.assertEqual(state.usage_attempts[0]['totalTokens'], 34)
        self.assertEqual(state.usage_attempts[0]['cost']['status'], 'infrastructure_cost_not_configured')

    def test_retry_is_one_logical_call_two_attempts_and_keeps_failed_usage(self):
        count = 0
        def handler(payload):
            nonlocal count
            count += 1
        original = httpx.AsyncClient
        def respond(request):
            handler(None)
            if count == 1: raise httpx.ReadTimeout('PRIVATE_ERROR_SENTINEL')
            return response(completion())
        config = settings(model_retry_base_delay_seconds=0)
        async def run():
            return await retry_transport(lambda n: AiClient(config).complete_async(MESSAGES), config,
                                         deadline=time.monotonic()+10, stage='reply.review', events=[])
        with observation_scope(reuse=False) as state, patch('app.services.ai.httpx.AsyncClient', side_effect=lambda **kw: original(transport=httpx.MockTransport(respond))):
            asyncio.run(run())
        snapshot = usage_snapshot(state)
        self.assertEqual((snapshot['logicalCalls'], snapshot['httpAttempts'], snapshot['retryAttempts']), (1, 2, 1))
        self.assertEqual(snapshot['usageCoverage'], .5)
        self.assertEqual([row['attemptNumber'] for row in snapshot['attempts']], [1, 2])
        self.assertNotIn('PRIVATE_ERROR_SENTINEL', json.dumps(snapshot))


class DurableUsageTests(ToolRuntimeFixture):
    def attempt(self, state, user_id=None):
        bind_ledger(state, self.sessions, user_id=user_id or self.user.id)
        with ModelAttempt(settings(), provider='openai', base_url='https://api.deepseek.com', model='deepseek-flash') as attempt:
            attempt.capture(completion())

    def test_attempt_is_durable_before_network_and_finalization_is_idempotent(self):
        with observation_scope(reuse=False, ensure_run=True) as state:
            bind_ledger(state, self.sessions, user_id=self.user.id)
            with ModelAttempt(settings(), provider='openai', base_url='https://api.deepseek.com', model='deepseek-flash') as attempt:
                with self.sessions() as db:
                    row = db.get(ModelUsageAttempt, attempt.row['id'])
                    self.assertEqual(json.loads(row.payload_json)['status'], 'started')
                attempt.capture(completion())
            persist_run(state, finalized=True); persist_run(state, finalized=False)
        with self.sessions() as db:
            self.assertEqual(db.query(ModelUsageAttempt).count(), 1)
            self.assertTrue(db.get(ModelUsageRun, state.ids['run_id']).finalized)
            self.assertEqual(db.get(ModelUsageAttempt, attempt.row['id']).user_id, self.user.id)
            self.assertEqual(usage_run_detail(db, state.ids['run_id'])['tokens']['totalTokens'], 120)

    def test_reports_recompute_attempt_totals_even_if_run_snapshot_is_stale(self):
        with observation_scope(reuse=False, ensure_run=True) as state:
            self.attempt(state)
        with self.sessions() as db:
            snapshot = json.loads(db.get(ModelUsageRun, state.ids['run_id']).payload_json)
            self.assertEqual(snapshot['unknownUsageAttempts'], 1)  # written before HTTP
            detail = usage_run_detail(db, state.ids['run_id'])
            self.assertEqual(detail['unknownUsageAttempts'], 0)
            self.assertEqual(list_usage_runs(db)[0]['tokens']['totalTokens'], 120)
            report = usage_report(db)
            self.assertEqual((report['httpAttempts'], report['unfinishedRuns']), (1, 1))

    def test_two_concurrent_users_do_not_share_runs_or_attempts(self):
        def run(user_id):
            with observation_scope(reuse=False, ensure_run=True) as state:
                self.attempt(state, user_id=user_id)
                persist_run(state, finalized=True)
                return state.ids['run_id']
        with ThreadPoolExecutor(2) as pool:
            ids = list(pool.map(run, [1001, 1002]))
        self.assertNotEqual(ids[0], ids[1])
        with self.sessions() as db:
            self.assertEqual([usage_run_detail(db, key)['userId'] for key in ids], [1001, 1002])
            self.assertTrue(all(usage_run_detail(db, key)['httpAttempts'] == 1 for key in ids))

    def test_ledger_failure_does_not_invent_successful_coverage(self):
        factory = Mock(side_effect=RuntimeError('PRIVATE_DATABASE_SENTINEL'))
        with observation_scope(reuse=False) as state:
            bind_ledger(state, factory)
            with ModelAttempt(settings(), provider='openai', base_url='https://api.deepseek.com', model='deepseek-flash'):
                pass
        snapshot = usage_snapshot(state)
        self.assertGreater(snapshot['ledgerWriteErrors'], 0)
        self.assertIsNone(snapshot['tokens']); self.assertFalse(snapshot['tokensComplete'])
        self.assertNotIn('PRIVATE_DATABASE_SENTINEL', json.dumps(snapshot))

    def test_online_and_indexing_costs_have_separate_scopes(self):
        for scope in ['online_chat', 'knowledge_index']:
            with observation_scope(reuse=False) as state:
                bind_ledger(state, self.sessions, scope=scope)
                with ModelAttempt(settings(), provider='openai', base_url='https://api.deepseek.com', model='deepseek-flash') as attempt:
                    attempt.capture(completion())
                persist_run(state, finalized=True)
        with self.sessions() as db:
            self.assertEqual(usage_report(db, scope='online_chat')['httpAttempts'], 1)
            self.assertEqual(usage_report(db, scope='knowledge_index')['httpAttempts'], 1)
            self.assertEqual(usage_report(db, scope='all')['httpAttempts'], 2)

    def test_redis_history_valid_absent_stale_error_and_unavailable(self):
        for status, version, expected in [('ok', 0, 'hits'), ('ok', None, 'misses'), ('ok', 99, 'misses'),
                                           ('error', None, 'errors'), ('unavailable', None, 'bypasses')]:
            store = Mock(snapshot_status=status)
            store.load_snapshot.return_value = ([], version)
            store.messages_from_rows.return_value = []
            with self.subTest(status=status, version=version), observation_scope(reuse=False) as state:
                SessionMemoryService(self.db, self.settings, store).load(self.user, self.session)
                self.assertEqual(usage_snapshot(state)['cacheLayers']['redis_session_history'][expected], 1)

    def test_redis_transport_exposes_error_and_unavailable_distinctly(self):
        store = RedisShortTermMemoryStore(self.settings)
        self.assertEqual(store.load_snapshot('test'), ([], None)); self.assertEqual(store.snapshot_status, 'unavailable')
        store.client = Mock(); store.client.pipeline.side_effect = RuntimeError('synthetic failure')
        self.assertEqual(store.load_snapshot('test'), ([], None)); self.assertEqual(store.snapshot_status, 'error')

    def test_document_embedding_reuse_and_embedding_batch_accounting(self):
        service = KnowledgeService.__new__(KnowledgeService)
        store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
        store.settings = settings(openai_embedding_model='qwen3.7-text-embedding', openai_embedding_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')
        store.can_embed = True; service.vector_store = store
        chunks = [KnowledgeChunk(content='existing', source='synthetic', source_index=0, embedding_json='[1,2]'),
                  KnowledgeChunk(content='missing', source='synthetic', source_index=1)]
        data = {'model': 'qwen3.7-text-embedding', 'data': [{'index': 0, 'embedding': [3, 4]}], 'usage': {'prompt_tokens': 10, 'total_tokens': 10}}
        with observation_scope(reuse=False) as state, patch('app.services.vector_store.httpx.post', return_value=response(data)) as post:
            service.settings = self.settings
            self.assertEqual(service._embeddings_for_chunks(chunks), [[1, 2], [3, 4]])
        self.assertEqual(post.call_count, 1)
        snapshot = usage_snapshot(state)
        self.assertEqual(snapshot['attempts'][0]['stage'], 'embedding.document')
        self.assertEqual(snapshot['tokens']['inputTokens'], 10)
        self.assertEqual(snapshot['cacheLayers']['document_embedding_reuse']['hitRate'], .5)

    def test_usage_api_is_admin_only_audited_bounded_and_metadata_only(self):
        self.user.password_hash = hash_password('fixture')
        admin = self.db.query(UserAccount).filter_by(username='admin').one(); admin.password_hash = hash_password('fixture'); self.db.commit()
        with observation_scope(reuse=False, ensure_run=True) as state:
            self.attempt(state); persist_run(state, finalized=True)
        app = FastAPI(); app.include_router(router); app.add_middleware(ObservationMiddleware)
        def db():
            with self.sessions() as session: yield session
        app.dependency_overrides[get_db] = db
        with TestClient(app) as client:
            for path in ['/api/admin/model-usage-summary', '/api/admin/model-usage', '/api/admin/model-usage/'+state.ids['run_id']]:
                self.assertEqual(client.get(path, auth=('student', 'fixture')).status_code, 403)
                result = client.get(path, auth=('admin', 'fixture'))
                self.assertEqual(result.status_code, 200)
                self.assertNotIn('PRIVATE_SENTINEL', result.text); self.assertNotIn('SECRET_KEY_SENTINEL', result.text)
            self.assertEqual(client.get('/api/admin/model-usage?limit=101', auth=('admin','fixture')).status_code, 422)
            self.assertEqual(client.get('/api/admin/model-usage-summary?hours=0', auth=('admin','fixture')).status_code, 422)
            self.assertEqual(client.get('/api/admin/model-usage?includeSensitive=true', auth=('admin','fixture')).status_code, 400)
        with self.sessions() as db:
            self.assertGreater(db.query(DataAccessAudit).filter_by(resource='model-usage', status='ALLOWED').count(), 0)
            self.assertGreater(db.query(DataAccessAudit).filter_by(resource='model-usage', status='DENIED').count(), 0)

    def test_csv_preserves_unknown_as_empty_and_neutralizes_formula_metadata(self):
        text = attempts_csv([{'id': 'test', 'actualModel': '=FORMULA()', 'inputTokens': None,
                              'cost': {'status': 'usage_unknown', 'amount': None}}])
        import csv, io
        row = next(csv.DictReader(io.StringIO(text)))
        self.assertEqual(row['inputTokens'], ''); self.assertEqual(row['amount'], '')
        self.assertEqual(row['actualModel'], "'=FORMULA()")


if __name__ == '__main__':
    unittest.main()
