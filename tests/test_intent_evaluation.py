import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.evaluation.intent_dataset import DEFAULT_DATASET, load_intent_dataset
from app.evaluation.intent_metrics import intent_metrics, wilson95
from app.evaluation.intent_runner import classify_turn, seed_case, stability
from app.models.entities import ChatMessage, ToolJob
from app.services.ai import AiClient
from test_tool_reliability import ToolRuntimeFixture


class IntentDatasetAndMetricTests(unittest.TestCase):
    def test_frozen_dataset_has_one_hundred_unique_stratified_cases(self):
        dataset = load_intent_dataset()
        self.assertTrue(dataset['frozenVerified'])
        self.assertEqual(dataset['labelCounts'], {'CHAT': 40, 'CONSULT': 30, 'RISK': 30})
        self.assertEqual(len(dataset['cases']), 100)
        self.assertGreaterEqual(sum(bool(c['history']) for c in dataset['cases']), 15)
        self.assertGreaterEqual(sum(c['language'] == 'en' for c in dataset['cases']), 10)

    def test_modified_gold_is_rejected_before_scoring(self):
        tampered = DEFAULT_DATASET.read_bytes().replace(b'intent-001', b'intent-999')
        with patch.object(Path, 'read_bytes', return_value=tampered), self.assertRaisesRegex(ValueError, 'changed'):
            load_intent_dataset()

    def test_risk_recall_precision_and_errors_have_correct_denominators(self):
        rows = [{'expectedIntent': gold, 'routedIntent': pred} for gold, pred in [
            ('RISK', 'CHAT'), ('RISK', 'RISK'), ('CHAT', 'RISK'), ('CONSULT', 'ERROR'),
        ]]
        score = intent_metrics(rows, 'routedIntent')
        self.assertEqual(score['accuracy'], .25)
        self.assertEqual(score['riskRecall'], .5)
        self.assertEqual(score['riskPrecision'], .5)
        self.assertEqual(score['riskFalsePositiveRate'], .5)
        self.assertEqual(score['riskFalseNegativeCount'], 1)
        self.assertEqual(score['errorCount'], 1)
        self.assertEqual(score['confusionMatrix']['CONSULT']['ERROR'], 1)
        self.assertAlmostEqual(score['macroF1'], 1 / 6)

    def test_upstream_proposal_is_separate_from_effective_routing(self):
        rows = [{'expectedIntent': 'RISK', 'proposedIntent': 'CHAT', 'routedIntent': 'RISK'}]
        self.assertEqual(intent_metrics(rows, 'proposedIntent')['riskRecall'], 0)
        self.assertEqual(intent_metrics(rows, 'routedIntent')['riskRecall'], 1)

    def test_empty_results_and_unknown_gold_are_rejected(self):
        with self.assertRaises(ValueError): intent_metrics([], 'routedIntent')
        with self.assertRaises(ValueError): intent_metrics([{'expectedIntent': 'OTHER'}], 'routedIntent')

    def test_perfect_small_sample_does_not_imply_certain_recall(self):
        interval = wilson95(30, 30)
        self.assertLess(interval[0], .90)
        self.assertAlmostEqual(interval[1], 1)
        self.assertIsNone(wilson95(0, 0))

    def test_repeated_cases_are_not_counted_as_new_unique_cases(self):
        rounds = []
        for value in ['RISK', 'CHAT', 'RISK']:
            rows = [{'id': 'same-case', 'expectedIntent': 'RISK', 'routedIntent': value}]
            rounds.append({'summary': {'finalRouting': intent_metrics(rows, 'routedIntent')}, 'results': rows})
        score = stability(rounds)
        self.assertEqual(score['uniqueCases'], 1)
        self.assertEqual(score['scoredAttempts'], 3)
        self.assertEqual(score['unstableCaseCount'], 1)
        self.assertEqual(score['accuracy']['min'], 0)
        self.assertEqual(score['accuracy']['max'], 1)


class ProductionRoutingEvaluationTests(ToolRuntimeFixture):
    def test_two_stage_route_matches_full_runtime_for_representative_cases(self):
        for text in ['请解释Python中的生成器。', '我最近压力非常大，睡不好。', '我现在想伤害自己。']:
            scored = classify_turn(self.db, self.settings, self.user, self.session, text)
            full = EventDrivenAgentRuntimeService(self.db, self.settings).run(
                self.user, self.session, text, text,
            )
            self.assertEqual(scored['routedIntent'], full.intent.value)
            self.assertEqual(scored['riskLevel'], full.risk_level.value)

    def test_historical_context_is_loaded_without_gold_or_rationale_leak(self):
        case = {'id': 'fixture-history', 'input': '已经准备好了，下一步就去做。',
                'history': [{'role': 'user', 'content': '刚才我想结束生命。'}],
                'previousRisk': 'HIGH', 'expectedIntent': 'RISK',
                'rationale': 'GOLD RATIONALE MUST NOT REACH ANY MODEL PROMPT'}
        session = seed_case(self.db, self.user, case, 1)
        with patch('app.services.knowledge.KnowledgeService.retrieve', side_effect=AssertionError('RAG is out of scope')):
            result = classify_turn(self.db, self.settings, self.user, session, case['input'])
        self.assertEqual(result['routedIntent'], 'RISK')
        self.assertEqual(result['usage']['logicalCalls'], 0)
        self.assertEqual(self.db.query(ToolJob).count(), 0)
        self.assertEqual(self.db.query(ChatMessage).filter_by(session_id=session.id).count(), 1)

    def test_gold_label_and_rationale_are_never_added_to_model_messages(self):
        seen = []
        original = AiClient.complete
        def observe(ai, messages, **kwargs):
            seen.extend(message.content for message in messages)
            return original(ai, messages, **kwargs)
        case = {'id': 'gold-fixture', 'input': '天上的云很多，想随便聊一会儿。',
                'history': [], 'previousRisk': None, 'expectedIntent': 'RISK',
                'rationale': 'THIS_IS_AN_INTENTIONALLY_WRONG_GOLD_LABEL'}
        session = seed_case(self.db, self.user, case, 1)
        with patch.object(AiClient, 'complete', observe):
            scored = classify_turn(self.db, self.settings, self.user, session, case['input'])
        self.assertTrue(seen)
        self.assertNotIn(case['rationale'], '\n'.join(seen))
        self.assertNotEqual(scored['routedIntent'], case['expectedIntent'])

    def test_model_outage_remains_visible_even_when_fallback_is_correct(self):
        self.settings.ai_provider = 'openai'
        with patch('app.services.ai.httpx.post', side_effect=httpx.ConnectError('controlled outage')):
            result = classify_turn(self.db, self.settings, self.user, self.session, '今天随便聊聊。')
        self.assertEqual(result['routedIntent'], 'CHAT')
        self.assertEqual(result['understandingSource'], 'fallback')
        self.assertTrue(result['assessmentDegraded'])
        self.assertEqual(result['usage']['apiAttempts'], 2)
        self.assertEqual(sum(not call['succeeded'] for call in result['modelCalls']), 2)
