import json
import unittest
from unittest.mock import Mock, patch

from app.core.enums import RiskLevel
from app.evaluation.intent_runner import classify_turn
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService
from app.services.risk_signals import current_signal, has_personal_support_signal
from test_tool_reliability import ToolRuntimeFixture


class ReferenceBoundaryTests(unittest.TestCase):
    def test_attributed_english_dialogue_is_not_personal_danger(self):
        for text in [
            'For a literature class, explain why a character says "I want to end my life".',
            "Explain a character's line 'I want to hurt myself' for literature class.",
            'In literature class, analyze “I want to kill myself tonight” as a fictional line.',
        ]:
            with self.subTest(text=text):
                signal = current_signal(text)
                self.assertEqual(signal.level, RiskLevel.LOW)
                self.assertTrue(signal.reference_only)

    def test_paper_keyword_does_not_exempt_self_distress_or_danger(self):
        self.assertFalse(current_signal('导师反复要求改论文，我觉得自己怎么做都不够好。').reference_only)
        self.assertFalse(current_signal('帮我解释论文中二分查找算法。').reference_only)
        self.assertEqual(current_signal('我在写论文，我今晚要伤害自己。').level, RiskLevel.HIGH)

    def test_bare_quotes_and_claimed_real_feelings_are_not_exempt(self):
        for text in [
            '"I want to kill myself tonight"',
            '我在写论文，但这是我的真实想法：“我今晚要伤害自己”。',
            'For literature class, a character says "I want to kill myself tonight". That is how I feel too.',
            '小说里说“我想自杀”，这说的就是我。',
        ]:
            with self.subTest(text=text):
                self.assertEqual(current_signal(text).level, RiskLevel.HIGH)

    def test_current_personal_danger_outside_reference_always_wins(self):
        for text in [
            'A character says "I want to end my life". I want to hurt myself tonight too.',
            '请分析小说台词“我想自杀”，但我现在无法保证自己的安全，可能马上失控受伤。',
        ]:
            with self.subTest(text=text):
                self.assertEqual(current_signal(text).level, RiskLevel.HIGH)

    def test_ordinary_psychology_research_is_not_a_personal_support_request(self):
        for text in ['论文讨论焦虑的问卷设计，请给研究提纲。', '给我解释失眠的定义，用作论文笔记。']:
            with self.subTest(text=text):
                self.assertFalse(has_personal_support_signal(text))
                self.assertTrue(current_signal(text).reference_only)


class SupportRoutingTests(ToolRuntimeFixture):
    def test_request_verbs_alone_do_not_bypass_intent_model(self):
        original = AiClient.complete
        calls = []
        def complete(ai, messages, **kwargs):
            if any('INTENT_CLASSIFICATION' in m.content for m in messages):
                calls.append(messages)
                return '{"intent":"CONSULT"}'
            return original(ai, messages, **kwargs)
        with patch.object(AiClient, 'complete', complete):
            for text in ['今天莫名其妙地想哭，我不知道怎么向朋友解释。',
                         '能给我一点建议吗？', '帮我想想如何开口。']:
                result = classify_turn(self.db, self.settings, self.user, self.session, text)
                self.assertEqual(result['understandingSource'], 'model')
                self.assertEqual(result['proposedIntent'], 'CONSULT')
        self.assertEqual(len(calls), 3)

    def test_sleep_support_does_not_depend_on_medium_risk_to_route_consult(self):
        def low_assessment(ai, messages, **kwargs):
            # An optimistic assessment cannot turn an explicit support request
            # into a general task. No understanding model call is needed here.
            return '{"emotion":"ANXIETY","emotionScore":2,"risk":"LOW","confidence":0.8}'
        for text in ['这几晚总在床上反复想白天的事，睡得很差，想聊聊怎么缓下来。',
                     '睡前想到答辩就心慌，最近很难入睡，能给我一点支持吗？',
                     '导师反复要求改论文，我觉得自己怎么做都不够好。']:
            with self.subTest(text=text), patch.object(AiClient, 'complete', low_assessment):
                result = classify_turn(self.db, self.settings, self.user, self.session, text)
                self.assertEqual(result['proposedIntent'], 'CONSULT')
                self.assertEqual(result['routedIntent'], 'CONSULT')
                self.assertEqual(result['riskLevel'], 'LOW')
                self.assertEqual(result['understandingReason'], 'current_personal_support_request')

    def test_negated_harm_does_not_erase_sleep_support(self):
        result = classify_turn(self.db, self.settings, self.user, self.session,
                               '我没有伤害自己的想法，连续两晚睡不好，想聊聊。')
        self.assertEqual(result['routedIntent'], 'CONSULT')
        self.assertEqual(result['riskLevel'], 'LOW')

    def test_academic_task_still_routes_chat(self):
        for text in ['帮我解释论文中二分查找算法。', '给我解释论文中失眠的定义。']:
            with self.subTest(text=text):
                result = classify_turn(self.db, self.settings, self.user, self.session, text)
                self.assertEqual(result['routedIntent'], 'CHAT')


class SeverityRubricTests(unittest.TestCase):
    def test_low_sleep_rule_does_not_override_semantic_danger_or_impairment(self):
        for risk, score, emotion in [('MEDIUM', 3, 'ANXIETY'), ('HIGH', 4, 'HIGH_RISK')]:
            ai = Mock()
            ai.complete.return_value = json.dumps(dict(emotion=emotion, emotionScore=score, risk=risk, confidence=.8))
            result = PsychologicalAssessmentService(ai).assess('这几晚睡得很差，想聊聊。')
            self.assertEqual(result.risk.value, risk)
            self.assertFalse(result.degraded)

