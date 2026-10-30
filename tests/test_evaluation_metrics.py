import math
import unittest

from app.rag_eval.metrics import score_ranking, aggregate
from app.rag_eval.dataset import load_dataset
from app.rag_eval.runner import check_regression
from app.services.assessment import PsychologicalAssessmentService
from app.schemas.dtos import AiMessage


class EvaluationMetricTests(unittest.TestCase):
    def test_recall_is_not_hit_rate_and_idcg_includes_missing_gold(self):
        score = score_ranking(['a'], {'a': 2, 'b': 2, 'c': 1}, 4)
        self.assertTrue(score['hit'])
        self.assertEqual(score['recallAtK'], 1 / 3)
        self.assertEqual(score['precisionAtK'], 1 / 4)
        self.assertLess(score['ndcgAtK'], 1)
        self.assertAlmostEqual(score['ndcgAtK'], 3 / (3 + 3 / math.log2(3) + 1 / math.log2(4)))

    def test_duplicates_cannot_inflate_metrics(self):
        score = score_ranking(['a', 'a', 'b'], {'a': 2, 'b': 1}, 3)
        self.assertEqual(score['recallAtK'], 1)
        self.assertEqual(score['precisionAtK'], 2 / 3)
        self.assertLess(score['ndcgAtK'], 1)

    def test_unanswerable_queries_are_separate_from_positive_macro(self):
        positive = score_ranking(['a'], {'a': 2}, 4)
        negative = score_ranking(['unrelated'], {}, 4)
        result = aggregate([positive, negative])
        self.assertIsNone(negative['recallAtK'])
        self.assertEqual(result['recallAtK'], 1)
        self.assertEqual(result['unanswerableEmptyRetrievalRate'], 0)
        self.assertEqual(result['answerableCases'], 1)

    def test_frozen_gold_covers_corpus_and_has_negative_cases(self):
        data = load_dataset('app/rag_eval/data/retrieval-v1.json')
        self.assertTrue(data['frozenVerified'])
        self.assertEqual(len(data['sources']), 11)
        self.assertEqual(len(data['queries']), 52)
        self.assertEqual({q['source'] for c in data['queries'] for q in c['qrels']}, set(data['sources']))

    def test_regression_gate_rejects_incomparable_contracts(self):
        with self.assertRaises(ValueError):
            check_regression({'datasetSha256': 'new'}, {'datasetSha256': 'old'})

    def test_invalid_k_is_rejected(self):
        with self.assertRaises(ValueError):
            score_ranking([], {'a': 1}, 0)


class RiskContextTests(unittest.TestCase):
    def ai(self, value):
        class Fixed:
            def complete(self, messages):
                if isinstance(value, Exception):
                    raise value
                return value
        return Fixed()

    def test_negation_reference_and_current_harm_are_distinguished(self):
        service = PsychologicalAssessmentService(self.ai(AssertionError('rule should decide')))
        self.assertEqual(service.assess('我没有自杀想法，只是写课程论文。').risk.value, 'LOW')
        self.assertEqual(service.assess('我不想死，只是有些紧张。').risk.value, 'LOW')
        self.assertEqual(service.assess('小说角色说“我想自杀”，请分析台词。').risk.value, 'LOW')
        self.assertEqual(service.assess('我没有伤害自己的想法，但我准备伤害室友。').risk.value, 'HIGH')
        self.assertEqual(service.assess('这是写小说：我今晚要伤害自己。').risk.value, 'HIGH')

    def test_contextual_continuation_and_resolution(self):
        service = PsychologicalAssessmentService(self.ai(AssertionError('rule should decide')))
        history = [AiMessage(role='user', content='我准备伤害自己。')]
        self.assertEqual(service.assess('现在就去做。', history, 'HIGH').risk.value, 'HIGH')
        self.assertEqual(service.assess('嗯。', history, 'HIGH').risk.value, 'MEDIUM')
        self.assertEqual(service.assess('我现在安全了，没有伤害自己的打算。', history, 'HIGH').risk.value, 'LOW')

    def test_invalid_model_output_is_explicitly_degraded(self):
        service = PsychologicalAssessmentService(self.ai('{"emotion":"NORMAL","emotionScore":NaN,"risk":"LOW","confidence":0.9}'))
        result = service.assess('今天想随便聊一聊。')
        self.assertTrue(result.degraded)
        self.assertEqual(result.source, 'fallback')
        self.assertIsNone(result.confidence)
