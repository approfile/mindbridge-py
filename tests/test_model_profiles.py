import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import text

from app.api.model_profiles import router
from app.core.database import get_db
from app.core.security import hash_password
from app.models.entities import KnowledgeChunk, UserAccount, UserModelProfile
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.agent_models import AgentModelRegistry, AGENT_MODEL_ALIASES
from app.services.knowledge import KnowledgeService
from app.services.model_profiles import (ModelProfileService, ProfileInput, ProfileError,
    ProfileConflict, catalog, probe_capabilities, snapshot_metadata)
from app.services.observability import observation_scope
from app.services.profile_middleware import ModelProfileMiddleware
from app.services.vector_store import VectorStoreUnavailable
from test_tool_reliability import ToolRuntimeFixture


def request(revision=0, key='synthetic-chat-key', embedding_key='synthetic-embedding-key', mode='api', **changes):
    data = {'expected_revision': revision, 'mode': mode, 'embedding_dimensions': 32,
            'chat': {'connection': 'deepseek', 'model': 'deepseek-flash', 'api_key': key},
            'embedding': {'connection': 'dashscope-embedding', 'model': 'synthetic-embedding', 'api_key': embedding_key}}
    if mode == 'selfhost':
        data['chat'] = {'connection': 'selfhost-chat', 'model': 'Qwen/Qwen3-8B'}
        data['embedding'] = {'connection': 'selfhost-embedding', 'model': 'Qwen/Qwen3-Embedding-0.6B'}
    data.update(changes)
    return ProfileInput.model_validate(data)


class ModelProfileTests(ToolRuntimeFixture):
    def setUp(self):
        super().setUp()
        self.settings.chroma_persist_dir = str(self.root / 'chroma')
        self.settings.chroma_snapshot_dir = str(self.root / 'snapshots')
        self.settings.selfhost_chat_base_url = 'http://selfhost.test:8000/v1'
        self.settings.selfhost_embedding_base_url = 'http://selfhost.test:8001/v1'
        self.settings.selfhost_api_key = 'synthetic-local-key'
        self.settings.openai_api_key = 'DEPLOYMENT_KEY_MUST_NOT_LEAK'
        self.settings.openai_embedding_api_key = 'DEPLOYMENT_EMBEDDING_MUST_NOT_LEAK'
        self.user.password_hash = hash_password('test-password')
        self.other = UserAccount(username='other', display_name='Other', password_hash=hash_password('test-password'))
        self.db.add(self.other)
        self.db.add_all([KnowledgeChunk(source='synthetic', source_index=i, content=f'Synthetic passage {i}',
                         embedding_json=json.dumps([9.] * 32)) for i in range(2)])
        self.db.commit()
        self.service = ModelProfileService(self.db, self.settings)
        self.calls = []
        self.failure = None
        self.dimensions = 32
        self.content = '{"ok":true}'
        self.business = False
        original_client, original_async = httpx.Client, httpx.AsyncClient
        self.transport = httpx.MockTransport(self.respond)
        for target, replacement in [
            ('httpx.post', lambda url, **kw: self.sync_post(original_client, url, **kw)),
            ('httpx.Client', lambda **kw: original_client(transport=self.transport, **kw)),
            ('httpx.AsyncClient', lambda **kw: original_async(transport=self.transport, **kw)),
        ]:
            factory = patch(target, replacement)
            factory.start(); self.addCleanup(factory.stop)

    def sync_post(self, client, url, **kwargs):
        timeout = kwargs.pop('timeout', 5)
        with client(transport=self.transport, timeout=timeout) as connection:
            return connection.post(url, **kwargs)

    def respond(self, req):
        data = json.loads(req.content) if req.content else {}
        self.calls.append({'host': req.url.host, 'path': req.url.path,
                           'authorization': req.headers.get('authorization'), 'body': data})
        if self.failure:
            raise httpx.ConnectError(self.failure, request=req)
        if self.business and req.url.host != 'selfhost.test':
            raise AssertionError('External model API disabled in full-selfhost contract test')
        if req.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'Qwen/Qwen3-8B', 'max_model_len': 8192}]})
        usage = {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}
        if req.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'data': [{'index': i, 'embedding': [1.0] + [0.1] * (self.dimensions-1)}
                                  for i, _ in enumerate(data['input'])], 'usage': {'prompt_tokens': 10}})
        content = (AiClient(self.settings)._mock([AiMessage(**m) for m in data['messages']])
                   if self.business else self.content)
        if data.get('stream'):
            chunks = [{'model': data['model'], 'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}],
                       'usage': usage}]
            return httpx.Response(200, text=''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n')
        return httpx.Response(200, json={'model': data['model'], 'choices': [{'message': {'content': content},
                                       'finish_reason': 'stop'}], 'usage': usage})

    def test_credentials_are_encrypted_masked_and_owner_scoped(self):
        saved = self.service.save(self.user.id, request())
        self.assertEqual(saved['revision'], 1)
        self.assertTrue(saved['chat']['hasKey'])
        self.assertNotIn('synthetic-chat-key', json.dumps(saved))
        raw = self.db.execute(text('SELECT config_json FROM user_model_profiles')).scalar()
        self.assertTrue(raw.startswith('mbenc:v1:'))
        self.assertNotIn('synthetic-chat-key', raw)
        self.assertEqual(self.service.public(self.other.id)['revision'], 0)

    def test_failure_never_replaces_active_profile(self):
        self.service.save(self.user.id, request())
        self.failure = 'SENSITIVE_UPSTREAM_BODY_WITH_KEY'
        with self.assertRaises(ProfileError) as caught:
            self.service.save(self.user.id, request(1, key='replacement'))
        self.assertNotIn(self.failure, str(caught.exception))
        self.assertEqual(self.service.resolve(self.user.id).openai_api_key, 'synthetic-chat-key')

    def test_index_failure_does_not_activate_candidate(self):
        self.service.save(self.user.id, request())
        with patch.object(KnowledgeService, 'rebuild_vector_index', side_effect=RuntimeError('PRIVATE')):
            with self.assertRaisesRegex(ProfileError, '索引建立失败'):
                self.service.save(self.user.id, request(1, embedding_key='new-embedding-key'))
        self.assertEqual(self.service.public(self.user.id)['revision'], 1)

    def test_optimistic_revision_rejects_stale_update_and_reset(self):
        self.service.save(self.user.id, request())
        with self.assertRaises(ProfileConflict): self.service.save(self.user.id, request())
        with self.assertRaises(ProfileConflict): self.service.reset(self.user.id, 2)
        self.service.reset(self.user.id, 1)
        self.assertEqual(self.service.public(self.user.id)['revision'], 2)
        self.assertEqual(self.service.resolve(self.user.id).ai_provider, 'mock')
        self.assertEqual(json.loads(self.service._row(self.user.id).config_json), {'mode':'deployment_default'})
        self.service.save(self.user.id, request(2))
        self.assertEqual(self.service.public(self.user.id)['revision'], 3)

    def test_revocation_during_save_prevents_old_candidate_from_resurrecting_key(self):
        self.service.save(self.user.id, request())
        original = self.service.test
        def revoke_during_test(user_id, incoming):
            result = original(user_id, incoming)
            self.service.reset(user_id, 1)
            return result
        with patch.object(self.service, 'test', revoke_during_test), self.assertRaises(ProfileConflict):
            self.service.save(self.user.id, request(1, key='key-must-stay-revoked'))
        self.assertEqual(self.service.public(self.user.id)['mode'], 'deployment_default')

    def test_empty_or_missing_embedding_key_never_inherits_chat_or_global_key(self):
        for embedding_key in ('', None):
            with self.assertRaises(ProfileError):
                self.service.candidate(self.user.id, request(embedding_key=embedding_key))
        self.assertFalse(self.calls)

    def test_omitted_keys_are_preserved_for_same_connection_only(self):
        self.service.save(self.user.id, request())
        self.service.save(self.user.id, request(1, key=None, embedding_key=None))
        self.assertEqual(self.service.resolve(self.user.id).openai_api_key, 'synthetic-chat-key')
        with self.assertRaises(ProfileError):
            self.service.candidate(self.user.id, request(2, chat={'connection':'dashscope-chat','model':'qwen-plus'}))

    def test_full_selfhost_covers_every_role_and_has_no_cloud_credential_fallback(self):
        self.settings.agent_model_safety_provider = 'ollama'
        self.settings.agent_model_safety_model = 'old-cloud-role'
        self.service.save(self.user.id, request(mode='selfhost'))
        snapshot = self.service.resolve(self.user.id)
        for role in AGENT_MODEL_ALIASES:
            client = AgentModelRegistry(snapshot).client_for(role)
            self.assertEqual(client.settings.openai_model, 'Qwen/Qwen3-8B')
            self.assertEqual(client.settings.openai_api_key, 'synthetic-local-key')
            self.assertEqual(client._openai_options(), {'chat_template_kwargs': {'enable_thinking': False}})
        self.assertEqual({call['host'] for call in self.calls}, {'selfhost.test'})
        self.assertEqual({call['authorization'] for call in self.calls}, {'Bearer synthetic-local-key'})
        self.assertNotIn('DEPLOYMENT_KEY', json.dumps(snapshot_metadata(snapshot)))

    def test_selfhost_outage_does_not_call_cloud(self):
        data = self.service.candidate(self.user.id, request(mode='selfhost'))
        snapshot = self.service.settings_for(self.user.id, data, 1)
        self.failure = 'offline'
        with self.assertRaises(ProfileError): probe_capabilities(snapshot)
        self.assertEqual({c['host'] for c in self.calls}, {'selfhost.test'})

    def test_mixed_cloud_embedding_is_rejected_in_full_selfhost(self):
        invalid = request(mode='selfhost').model_dump()
        invalid['embedding'] = {'connection':'dashscope-embedding','model':'synthetic','api_key':'cloud-key'}
        with self.assertRaises(ProfileError):
            self.service.candidate(self.user.id, ProfileInput.model_validate(invalid))

    def test_snapshot_survives_profile_change_and_all_concurrent_calls_keep_own_keys(self):
        self.service.save(self.user.id, request(key='first-user-key'))
        self.service.save(self.other.id, request(key='second-user-key'))
        first, second = self.service.resolve(self.user.id), self.service.resolve(self.other.id)
        self.service.save(self.user.id, request(1, key='next-version-key', embedding_key=None))
        self.assertEqual(first.model_profile_revision, 1)
        self.calls.clear()
        def run(snapshot):
            for role in AGENT_MODEL_ALIASES:
                AgentModelRegistry(snapshot).client_for(role).complete([AiMessage(role='user',content='Synthetic')])
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(run, [first, second]))
        self.assertEqual({c['authorization'] for c in self.calls}, {'Bearer first-user-key','Bearer second-user-key'})
        self.assertEqual(self.settings.openai_api_key, 'DEPLOYMENT_KEY_MUST_NOT_LEAK')

    def test_scoped_index_ignores_legacy_vectors_and_binds_owner_model_credentials_and_corpus(self):
        self.service.save(self.user.id, request())
        one = KnowledgeService(self.db, self.service.resolve(self.user.id))
        self.assertEqual(one.vector_store.count(), 2)
        self.assertTrue(any(c['body'].get('input') == ['Synthetic passage 0','Synthetic passage 1'] for c in self.calls))
        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.id).all()
        self.assertEqual(json.loads(rows[0].embedding_json), [9.] * 32)
        self.calls.clear()
        one.rebuild_vector_index()
        self.assertFalse(self.calls)  # Account/model-bound document cache hit.
        self.service.save(self.other.id, request())
        other = KnowledgeService(self.db, self.service.resolve(self.other.id))
        self.assertNotEqual(one.vector_store.collection_name, other.vector_store.collection_name)
        self.service.save(self.user.id, request(1, embedding_key='new-embedding-key'))
        two = KnowledgeService(self.db, self.service.resolve(self.user.id))
        self.assertNotEqual(one.vector_store.collection_name, two.vector_store.collection_name)
        rows[0].content = 'Updated synthetic passage'; self.db.commit()
        three = KnowledgeService(self.db, self.service.resolve(self.user.id))
        self.assertNotEqual(two.vector_store.collection_name, three.vector_store.collection_name)

    def test_capability_probe_rejects_invalid_json_thinking_and_dimension(self):
        for content, dimension in [('not-json',32), ('<think>private</think>{"ok":true}',32), ('{"ok":true}',33)]:
            self.content, self.dimensions = content, dimension
            with self.assertRaises(ProfileError): self.service.test(self.user.id, request())
        self.assertEqual(self.service.public(self.user.id)['revision'], 0)

    def test_ingestion_builds_new_corpus_without_mutating_inflight_index(self):
        self.service.save(self.user.id, request())
        service = KnowledgeService(self.db, self.service.resolve(self.user.id))
        old_store = service.vector_store
        old_documents = old_store.collection.get()['documents']
        service.ingest('synthetic', 'Replacement synthetic passage.')
        self.assertNotEqual(old_store.collection_name, service.vector_store.collection_name)
        self.assertEqual(old_store.collection.get()['documents'], old_documents)
        self.assertEqual(service.vector_store.count(), 1)

    def test_admin_connection_change_requires_revalidation(self):
        self.service.save(self.user.id, request(mode='selfhost'))
        self.settings.selfhost_model_revision = 'changed-artifact'
        with self.assertRaises(ProfileError): self.service.resolve(self.user.id)

    def test_usage_attempt_records_profile_revision_without_credentials(self):
        self.service.save(self.user.id, request())
        with observation_scope(reuse=False) as state:
            AiClient(self.service.resolve(self.user.id)).complete([AiMessage(role='user',content='Synthetic')])
        attempt = state.usage_attempts[0]
        self.assertEqual(attempt['modelProfile']['revision'], 1)
        self.assertNotIn('synthetic-chat-key', json.dumps(attempt))

    def test_full_selfhost_chat_and_consult_use_snapshot_through_harness_reply_review_and_rag(self):
        from app.services.chat import ChatService
        from app.schemas.dtos import ChatRequest
        from app.models.entities import AgentRunTrace
        self.service.save(self.user.id, request(mode='selfhost'))
        self.business = True
        self.calls.clear()
        for message in ['你好，请帮我安排学习。', '这几天考试压力有点大，晚上睡不好。']:
            service = ChatService(self.db, self.settings)
            async def collect():
                return [block async for block in service.stream_chat(self.user.id, ChatRequest(message=message))]
            events = self.events(asyncio.run(collect()))
            self.assertEqual(events[-1]['type'], 'done', events)
        self.assertTrue(any(c['path'].endswith('/embeddings') for c in self.calls))
        self.assertTrue(any(c['body'].get('stream') for c in self.calls))
        self.assertTrue(any('FINAL_REPLY_REVIEW' in m.get('content','') for c in self.calls for m in c['body'].get('messages',[])))
        self.assertEqual({c['host'] for c in self.calls}, {'selfhost.test'})
        self.assertEqual({c['authorization'] for c in self.calls}, {'Bearer synthetic-local-key'})
        traces = self.db.query(AgentRunTrace).filter_by(user_id=self.user.id).all()
        self.assertEqual(len(traces), 2)
        for trace in traces:
            self.assertNotIn('synthetic-local-key', trace.agent_steps_json)
            observation = next(e['data'] for e in json.loads(trace.agent_steps_json) if e.get('kind') == 'observability')
            self.assertEqual(observation['modelProfile']['mode'], 'selfhost')
            self.assertEqual(observation['modelProfile']['revision'], 1)

    def test_validation_errors_do_not_echo_raw_credentials(self):
        from app.main import create_app
        # No lifespan/startup: all HTTP resources stay in this fixture.
        app = create_app()
        def db():
            with self.sessions() as session: yield session
        app.dependency_overrides[get_db] = db
        client = TestClient(app)
        self.addCleanup(client.close)
        data = request().model_dump(mode='json')
        data['chat']['api_key'] = 'SECRET_VALIDATION_SENTINEL_' * 170
        response = client.put('/api/model-profile', auth=('student','test-password'), json=data)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('SECRET_VALIDATION_SENTINEL', response.text)

    def test_deployment_default_namespace_changes_when_embedding_identity_changes(self):
        from app.services.vector_store import bind_embedding_namespace
        self.settings.knowledge_vector_enabled = True
        first = bind_embedding_namespace(self.settings)
        self.settings.openai_embedding_model = 'different-same-dimension-model'
        second = bind_embedding_namespace(self.settings)
        self.assertNotEqual(first.embedding_index_scope, second.embedding_index_scope)
        self.assertFalse(self.settings.embedding_index_scope)

    def test_api_requires_auth_and_exposes_only_current_account(self):
        self.service.save(self.user.id, request())
        app = FastAPI()
        app.include_router(router)
        app.add_middleware(ModelProfileMiddleware)
        def db():
            with self.sessions() as session: yield session
        app.dependency_overrides[get_db] = db
        with patch('app.api.model_profiles.get_settings', return_value=self.settings), TestClient(app) as client:
            self.assertEqual(client.get('/api/model-profile').status_code, 401)
            body = client.get('/api/model-profile', auth=('other','test-password')).json()
            self.assertEqual(body['revision'], 0)
            body = client.get('/api/model-profile', auth=('student','test-password')).json()
            self.assertTrue(body['chat']['hasKey'])
            self.assertNotIn('synthetic-chat-key', json.dumps(body))
            response = client.put('/api/model-profile', auth=('student','test-password'), content='x'*17000)
            self.assertEqual(response.status_code, 413)

    def test_catalog_never_exposes_service_token_or_arbitrary_urls(self):
        self.assertNotIn('synthetic-local-key', json.dumps(catalog(self.settings)))
        self.assertNotIn('http://', json.dumps(catalog(self.settings)))
        with self.assertRaises(ProfileError):
            self.service.candidate(self.user.id, request(chat={'connection':'http://169.254.169.254','model':'x','api_key':'x'}))

    def test_deployment_commands_share_gpu_budget_and_keep_loopback_auth(self):
        path = Path(__file__).resolve().parents[1] / 'deploy/autodl/serve.py'
        spec = importlib.util.spec_from_file_location('autodl_serve', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        commands = [module.command(role, {'path':'/models/model','id':role}) for role in ('chat','embedding')]
        self.assertLess(sum(float(c[c.index('--gpu-memory-utilization')+1]) for c in commands), 1)
        for command in commands:
            self.assertEqual(command[command.index('--host')+1], '127.0.0.1')
            self.assertNotIn('--api-key', command)
            self.assertIn('--no-enable-log-requests', command)
