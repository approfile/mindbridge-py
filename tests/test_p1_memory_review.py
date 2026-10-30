import asyncio
import hashlib
import json
from unittest.mock import patch

from app.agents.harness import MindBridgeAgentHarness
from app.core.enums import MessageRole
from app.models.entities import AgentRunTrace, ChatMessage, ChatSession, SessionMemory, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.ai import AiClient
from app.services.chat import ChatService
from app.services.response_review import deterministic_violations, safe_template
from app.services.session_memory import SessionMemoryService
from test_tool_reliability import ToolRuntimeFixture


class SessionMemoryTests(ToolRuntimeFixture):
    def save(self, content, role=MessageRole.USER):
        MindBridgeAgentHarness(self.db, self.settings).save_message(self.user, self.session, role, content)

    def load(self, current=''):
        return SessionMemoryService(self.db, self.settings).load(self.user, self.session, current)

    def test_chat_receives_prior_history_and_fact_sources(self):
        self.save('请记住，项目代号是星河73。')
        outcome = MindBridgeAgentHarness(self.db, self.settings).run(
            self.user, ChatRequest(message='项目代号是什么？', sessionId=self.session.public_id),
        )
        self.assertTrue(outcome.plan_accepted)
        self.assertTrue(any('星河73' in row.content for row in outcome.response_messages))
        snapshot = self.load()
        fact = snapshot.facts['项目代号']
        row = self.db.get(ChatMessage, fact['sourceMessageId'])
        self.assertEqual(row.role, 'USER')
        self.assertIn(fact['value'], row.content)

    def test_correction_replaces_old_fact_and_assistant_cannot_invent_memory(self):
        self.save('项目代号是甲一。')
        self.save('更正一下，项目代号改为乙二。')
        self.save('项目代号是模型幻觉。', MessageRole.ASSISTANT)
        memory = self.load('项目代号')
        self.assertEqual(memory.facts['项目代号']['value'], '乙二')
        self.assertNotIn('甲一', memory.brief)
        self.assertNotIn('模型幻觉', memory.brief)

    def test_fact_survives_window_eviction_and_database_reload(self):
        self.settings.redis_memory_max_messages = 6
        self.settings.memory_compaction_recent_messages = 4
        self.settings.chat_history_limit = 3
        self.save('项目代号是星河73。')
        for i in range(45):
            self.save(f'普通聊天第{i}条。')
        snapshot = self.load('项目代号是什么')
        self.assertEqual(snapshot.facts['项目代号']['value'], '星河73')
        self.assertLessEqual(len(snapshot.history), 6)
        self.assertLessEqual(len(snapshot.model_history), 5)
        self.assertIn('星河73', snapshot.model_history[0].content)
        self.assertLessEqual(len(snapshot.brief), self.settings.memory_summary_max_chars)

    def test_stale_cache_falls_back_and_repairs_version(self):
        self.save('项目代号是星河73。')
        class Stale:
            replaced = None
            def load_snapshot(self, sid):
                return [AiMessage(role='user', content='stale value')], -1
            def messages_from_rows(self, rows):
                return [AiMessage(role=r.role.lower(), content=r.content) for r in rows]
            def replace(self, sid, history, last_message_id=None):
                self.replaced = last_message_id
        store = Stale()
        memory = SessionMemoryService(self.db, self.settings, store).load(self.user, self.session)
        self.assertEqual(memory.source, 'database')
        self.assertEqual(store.replaced, memory.last_message_id)
        self.assertNotIn('stale value', str(memory.history))

    def test_user_and_session_isolation(self):
        self.save('项目代号是星河73。')
        other = UserAccount(username='other', display_name='Other', password_hash='unused')
        self.db.add(other)
        separate = ChatSession(public_id='separate-session', user_id=self.user.id, title='Separate')
        self.db.add(separate)
        self.db.commit()
        with self.assertRaises(ValueError):
            SessionMemoryService(self.db, self.settings).load(other, self.session)
        isolated = SessionMemoryService(self.db, self.settings).load(self.user, separate)
        self.assertEqual(isolated.history, [])
        self.assertEqual(isolated.facts, {})

    def test_privacy_and_forgetting_in_derived_state(self):
        self.save('项目名称是13800138000。')
        memory = self.load()
        self.assertNotIn('13800138000', memory.brief)
        self.assertNotIn('13800138000', json.dumps(memory.facts))
        self.save('请忘记项目名称。')
        self.assertNotIn('项目名称', self.load().facts)

    def test_legacy_messages_are_reconciled_in_id_order(self):
        for value in ['项目代号是甲一。', '项目代号改为乙二。']:
            self.db.add(ChatMessage(user_id=self.user.id, session_id=self.session.id, role='USER', content=value))
        self.db.commit()
        self.assertEqual(self.load().facts['项目代号']['value'], '乙二')

    def test_corrupt_derived_state_is_rebuilt_from_own_persisted_rows(self):
        self.save('项目代号是星河73。')
        memory = self.db.query(SessionMemory).filter_by(session_id=self.session.id).one()
        memory.state_json = '{"facts":"invalid"}'
        memory.last_message_id = 999999
        self.db.commit()
        self.assertEqual(self.load().facts['项目代号']['value'], '星河73')

    def test_recent_risk_context_and_explicit_safety_update(self):
        self.report('HIGH')
        self.save('我刚才想伤害自己。')
        harness = MindBridgeAgentHarness(self.db, self.settings)
        continuation = harness.run(self.user, ChatRequest(message='我已经准备好了，现在就去做。', sessionId=self.session.public_id))
        self.assertEqual(continuation.risk_level, 'HIGH')
        resolved = harness.run(self.user, ChatRequest(message='我现在已经安全了，室友在身边，没有伤害自己的打算。', sessionId=self.session.public_id))
        self.assertEqual(resolved.risk_level, 'LOW')
        self.assertEqual(self.load().previous_risk, 'LOW')


class FinalReplyReviewTests(ToolRuntimeFixture):
    def stream(self, service):
        async def collect():
            return [chunk async for chunk in service.stream_chat(
                self.user, ChatRequest(message='我不想活了。', sessionId=self.session.public_id),
            )]
        return self.events(asyncio.run(collect()))

    def reply_events(self):
        trace = self.rows(AgentRunTrace)[-1]
        return [e for e in json.loads(trace.agent_steps_json) if e.get('kind') in {'response_review', 'response_outcome'}]

    def test_rejected_body_is_never_streamed_and_one_revision_can_succeed(self):
        service = ChatService(self.db, self.settings)
        versions = iter(['你已经确诊抑郁症。风险等级：HIGH。', safe_template('HIGH')])
        calls = []
        async def generate(messages):
            calls.append(messages)
            yield next(versions)
        with patch.object(service.ai, 'stream', generate):
            events = self.stream(service)
        displayed = ''.join(e.get('content') or '' for e in events)
        self.assertNotIn('确诊', displayed)
        self.assertNotIn('HIGH', displayed)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[-1]['type'], 'done')
        reviews = self.reply_events()
        self.assertEqual([r['approved'] for r in reviews if r['kind'] == 'response_review'], [False, True])
        self.assertEqual(reviews[-1]['type'], 'FINAL_ACCEPTED')
        self.assertEqual(reviews[-1]['revisions'], 1)
        self.assertEqual(reviews[-1]['finalSha256'], hashlib.sha256(displayed.encode()).hexdigest())

    def test_two_rejections_use_fixed_fallback_and_do_not_claim_acceptance(self):
        service = ChatService(self.db, self.settings)
        calls = []
        async def generate(messages):
            calls.append(1)
            yield '你已经确诊抑郁症。'
        with patch.object(service.ai, 'stream', generate):
            events = self.stream(service)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[-1]['type'], 'error')
        self.assertNotIn('确诊', ''.join(e.get('content') or '' for e in events))
        self.assertEqual(self.reply_events()[-1]['status'], 'REVISION_BUDGET_EXHAUSTED')
        self.assertFalse(any(e.get('type') == 'FINAL_ACCEPTED' for e in self.reply_events()))

    def test_independent_model_rejection_requests_only_one_revision(self):
        service = ChatService(self.db, self.settings)
        versions = iter([safe_template('HIGH') + '初稿', safe_template('HIGH') + '修订稿'])
        decisions = iter(['{"approved":false,"violations":["EXCLUSIVE_DEPENDENCE"]}',
                          '{"approved":true,"violations":[]}'])
        async def generate(messages):
            yield next(versions)
        async def review(ai, messages):
            return next(decisions)
        with patch.object(service.ai, 'stream', generate), patch.object(AiClient, 'complete_async', review):
            events = self.stream(service)
        displayed = ''.join(e.get('content') or '' for e in events)
        self.assertNotIn('初稿', displayed)
        self.assertIn('修订稿', displayed)
        self.assertEqual(self.reply_events()[-1]['revisions'], 1)

    def test_review_completes_before_first_body_byte(self):
        service = ChatService(self.db, self.settings)
        approved = []
        async def generate(messages):
            yield safe_template('HIGH')
        async def review(ai, messages):
            self.assertIn(safe_template('HIGH'), messages[-1].content)
            approved.append(True)
            return '{"approved":true,"violations":[]}'
        async def collect():
            result = []
            async for chunk in service.stream_chat(self.user, ChatRequest(message='我不想活了。', sessionId=self.session.public_id)):
                if 'event: token' in chunk:
                    self.assertTrue(approved)
                result.append(chunk)
            return result
        with patch.object(service.ai, 'stream', generate), patch.object(AiClient, 'complete_async', review):
            asyncio.run(collect())

    def test_invalid_review_is_fail_closed_without_unbounded_retries(self):
        service = ChatService(self.db, self.settings)
        calls = []
        async def generate(messages):
            calls.append(1)
            yield safe_template('HIGH')
        async def invalid(ai, messages):
            return '{"approved":"true","violations":[]}'
        with patch.object(service.ai, 'stream', generate), patch.object(AiClient, 'complete_async', invalid):
            events = self.stream(service)
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.reply_events()[-1]['status'], 'REVIEW_UNAVAILABLE_OR_INVALID')

    def test_planning_budget_exhaustion_never_calls_generator_and_keeps_outbox(self):
        self.settings.agent_max_rounds = 0
        service = ChatService(self.db, self.settings)
        called = []
        async def forbidden(messages):
            called.append(True)
            yield 'must not display'
        with patch.object(service.ai, 'stream', forbidden):
            events = self.stream(service)
        self.assertEqual(called, [])
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(self.reply_events()[-1]['status'], 'PLANNING_BUDGET_EXHAUSTED')
        from app.models.entities import ToolJob
        self.assertEqual(len(self.rows(ToolJob)), 3)

    def test_reply_timeout_is_bounded(self):
        self.settings.reply_timeout_seconds = .01
        service = ChatService(self.db, self.settings)
        async def delayed(messages):
            await asyncio.sleep(1)
            yield 'late'
        with patch.object(service.ai, 'stream', delayed):
            events = self.stream(service)
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(self.reply_events()[-1]['status'], 'REPLY_TIMEOUT')

    def test_response_model_override_is_used_by_actual_generator(self):
        self.settings.ai_provider = 'openai'
        self.settings.agent_model_response_model = 'response-specific-model'
        self.assertEqual(ChatService(self.db, self.settings).ai.settings.openai_model, 'response-specific-model')

    def test_body_rules_distinguish_boundary_explanation_from_diagnosis(self):
        self.assertIn('DIAGNOSIS', deterministic_violations('你已经确诊抑郁症。', 'LOW'))
        self.assertNotIn('DIAGNOSIS', deterministic_violations('不能仅凭这些判断你患有抑郁症。', 'LOW'))
        self.assertIn('UNSUPPORTED_RESOURCE', deterministic_violations('请拨打心理中心电话010-12345678。', 'LOW'))
        self.assertNotIn('UNSUPPORTED_RESOURCE', deterministic_violations('请拨打电话010-12345678。', 'LOW', '电话010-12345678'))
