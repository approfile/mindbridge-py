import json
import unittest
from unittest.mock import patch

import httpx

from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.evaluation.intent_runner import classify_turn
from app.models.entities import AgentRunTrace, ToolJob
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.ai import AiClient, PromptTemplates
from app.services.intent_contract import parse_intent
from app.services.risk_signals import analyze_risk, current_signal
from test_tool_reliability import ToolRuntimeFixture


class PersonalSafetyRuleTests(unittest.TestCase):
    def test_unsafe_is_not_safe_and_imminence_is_not_lost(self):
        for text in ['我现在无法保证自己的安全，有个强烈冲动要马上让自己受伤。',
                     '我目前不能确保自己安全，我可能立刻失控。',
                     'I cannot promise to stay safe. I might act right now.']:
            with self.subTest(text=text):
                self.assertEqual(current_signal(text).level.value, 'HIGH')
        for text in ['我目前不敢保证自己是安全的。', '我不觉得现在安全。',
                     '我不确定自己是否安全。', 'I cannot guarantee my safety.']:
            with self.subTest(text=text):
                result = current_signal(text)
                self.assertEqual(result.level.value, 'MEDIUM')
                self.assertTrue(result.needs_clarification)
                self.assertEqual(analyze_risk(text, previous_risk='HIGH').level.value, 'HIGH')

    def test_real_safety_updates_are_preserved_but_questions_are_not_assertions(self):
        for text in ['我现在已经安全了，朋友在旁边。',
                     '之前我没法保证安全，不过我现在已经安全了。',
                     '以前有过自伤经历，但现在没有这个想法，正在和咨询师沟通。',
                     'I am safe now and I no longer want to hurt myself.']:
            with self.subTest(text=text):
                self.assertEqual(current_signal(text).level.value, 'LOW')
        for text in ['如果我现在安全了应该做什么？', '我希望我现在安全。', '我现在安全了吗？',
                     'Can you confirm whether I am safe now?']:
            with self.subTest(text=text):
                result = current_signal(text)
                self.assertNotEqual(result.reason, 'explicit_current_safety_update')

    def test_current_danger_beats_reassuring_words(self):
        result = current_signal('我现在安全了，但我决定马上伤害自己。')
        self.assertEqual(result.level.value, 'HIGH')

    def test_technical_safety_is_not_personal_safety(self):
        for text in ['我无法保证代码安全，帮我检查程序。', '我现在不能保证账号安全，想重置密码。']:
            with self.subTest(text=text):
                self.assertIsNone(current_signal(text).level)

    def test_anaphoric_immediate_action_requires_dangerous_context(self):
        for text in ['我还是打算马上这么做。', '我仍然决定立刻这样做。', 'I will do it immediately.']:
            with self.subTest(text=text):
                self.assertEqual(analyze_risk(text, previous_risk='HIGH').level.value, 'HIGH')
                self.assertNotEqual(analyze_risk(text).level.value if analyze_risk(text).level else None, 'HIGH')
                history = [AiMessage(role='user', content='我刚才打算伤害自己。')]
                self.assertEqual(analyze_risk(text, history).level.value, 'HIGH')

    def test_retraction_and_benign_preparation_are_not_action_continuation(self):
        for text in ['我不会这么做了，先请室友陪我。', '我不打算这样做了。', 'I will not do it.']:
            with self.subTest(text=text):
                self.assertEqual(analyze_risk(text, previous_risk='HIGH').level.value, 'LOW')
        self.assertNotEqual(analyze_risk('我准备好了，马上去上课。', previous_risk='HIGH').level, 'HIGH')


class IntentContractTests(unittest.TestCase):
    def test_exact_schema_and_enum_only(self):
        for label in ['CHAT', 'CONSULT', 'RISK']:
            self.assertEqual(parse_intent(json.dumps({'intent': label})).value, label)
        for raw in ['CONSULT', '意图：CONSULT', '{"intent":"UNKNOWN"}', '{"intent":"consult"}',
                    '{"intent":null}', '{"intent":"CHAT","reason":"RISK"}',
                    '{"intent":"RISK","intent":"CHAT"}', '{"intent":"CHAT"} RISK',
                    '{"intent":NaN}', '```json\n{"intent":"CONSULT"}\n```']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_intent(raw)

    def test_intent_json_mode_is_sent_to_each_real_transport(self):
        for provider in ['openai', 'ollama']:
            settings = Settings(_env_file=None, ai_provider=provider, openai_base_url='https://example.test',
                                openai_api_key='test')
            response = httpx.Response(200, request=httpx.Request('POST', 'https://example.test'), json={
                'choices': [{'message': {'content': '{"intent":"CHAT"}'}, 'finish_reason': 'stop'}],
                'message': {'content': '{"intent":"CHAT"}'},
            })
            with patch('app.services.ai.httpx.post', return_value=response) as post:
                AiClient(settings).complete(PromptTemplates.intent_prompt([], '随便聊聊。'), json_mode=True)
                payload = post.call_args.kwargs['json']
                if provider == 'openai': self.assertEqual(payload['response_format'], {'type': 'json_object'})
                else: self.assertEqual(payload['format'], 'json')
                AiClient(settings).complete([AiMessage(role='user', content='ordinary completion')])
                payload = post.call_args.kwargs['json']
                self.assertNotIn('response_format', payload)
                self.assertNotIn('format', payload)


class RuntimeContractAndOutboxTests(ToolRuntimeFixture):
    def test_valid_json_is_model_source_and_invalid_narrative_is_visible_fallback(self):
        original = AiClient.complete
        def replacement(raw):
            def complete(ai, messages, **kwargs):
                if any('INTENT_CLASSIFICATION' in m.content for m in messages): return raw
                return original(ai, messages, **kwargs)
            return complete
        with patch.object(AiClient, 'complete', replacement('{"intent":"CONSULT"}')):
            value = classify_turn(self.db, self.settings, self.user, self.session, '最近很失落，想找人说说话。')
        self.assertEqual(value['proposedIntent'], 'CONSULT')
        self.assertEqual(value['understandingSource'], 'model')
        with patch.object(AiClient, 'complete', replacement('意图：CONSULT，理由是情绪失落。')):
            value = classify_turn(self.db, self.settings, self.user, self.session, '最近很失落，想找人说说话。')
        self.assertEqual(value['understandingSource'], 'fallback')
        self.assertEqual(value['understandingReason'], 'intent_unavailable_or_invalid')
        self.assertIsNotNone(value['understandingErrorType'])

    def test_both_reproduced_leaks_persist_high_risk_outbox_and_trace_reason(self):
        harness = MindBridgeAgentHarness(self.db, self.settings)
        first = harness.run(self.user, ChatRequest(
            message='我现在无法保证自己的安全，有个强烈冲动要马上让自己受伤。', sessionId=self.session.public_id))
        second = harness.run(self.user, ChatRequest(message='我还是打算马上这么做。', sessionId=self.session.public_id))
        for outcome in [first, second]:
            self.assertEqual(outcome.intent.value, 'RISK')
            self.assertEqual(outcome.risk_level, 'HIGH')
            jobs = self.db.query(ToolJob).filter_by(report_id=outcome.report_id).all()
            self.assertEqual({job.kind for job in jobs}, {'EXCEL_REPORT', 'CASE_CREATE', 'ALERT_SEND'})
            trace = self.db.get(AgentRunTrace, outcome.trace_id)
            intents = [x['payload'] for x in json.loads(trace.agent_steps_json)
                       if x.get('kind') == 'agent_artifact' and x.get('artifactKind') == 'intent']
            self.assertEqual(intents[-1]['source'], 'rule')
            self.assertFalse(intents[-1]['degraded'])
