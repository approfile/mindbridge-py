"""No network: judge provenance, RAGAS reporting and human-input boundaries."""
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
judge=load('offline_judge','scripts/judge_agent_layers.py')
ragas_report=load('ragas_reporting','scripts/score_ragas_layers.py')


class LayeredReportingTests(unittest.TestCase):
    def decision(self):return {k:{'verdict':'pass','reason':'A short evidence-based judgment','quotes':['actual reply']} for k in ['rubric','safety','claims']}

    def test_judge_quotes_must_come_from_visible_reply(self):
        d=self.decision();d['safety']['quotes']=['a user-input-only safety word']
        with self.assertRaises(ValueError):judge.validate_judge_output(d,'actual reply')

    def test_positive_judgment_requires_a_quote_if_reply_exists(self):
        d=self.decision();d['claims']['quotes']=[]
        with self.assertRaises(ValueError):judge.validate_judge_output(d,'actual reply')

    def test_invalid_judge_verdict_cannot_pass(self):
        d=self.decision();d['rubric']['verdict']=True
        with self.assertRaises(ValueError):judge.validate_judge_output(d,'actual reply')

    def test_unknown_judge_can_defer_without_manufactured_quote(self):
        d={k:{'verdict':'unknown','reason':'Insufficient evidence','quotes':[]} for k in ['rubric','safety','claims']}
        self.assertEqual(judge.validate_judge_output(d,'actual reply'),d)

    def test_pending_form_is_not_a_human_submission(self):
        with self.assertRaises(ValueError):judge.validate_human_submission({'origin':'human_user_pending','samples':[]},['H01'])

    def test_true_human_unknown_remains_unknown(self):
        form={'origin':'human_user_submitted','reviewerId':'test-human','submittedAt':'2026-10-28',
          'samples':[{'sampleId':'H01','rubricVerdict':'unknown','safetyVerdict':'pass','completionClaimVerdict':'pending'}]}
        rows=judge.validate_human_submission(form,['H01']);self.assertEqual(rows[0]['rubricVerdict'],'unknown')

    def test_human_sample_ids_cannot_be_duplicated(self):
        form={'origin':'human_user_submitted','reviewerId':'test-human','submittedAt':'2026-10-28','samples':[
          {'sampleId':'H01','rubricVerdict':'pass','safetyVerdict':'pass','completionClaimVerdict':'pass'}]*2}
        with self.assertRaises(ValueError):judge.validate_human_submission(form,['H01','H02'])

    def test_ragas_failures_are_not_replaced_with_zero_or_dropped(self):
        rows=[{'sampleId':'a','metric':'faithfulness','status':'scored','value':.8},
              {'sampleId':'b','metric':'faithfulness','status':'failed','value':None}]
        r=ragas_report.summarize_rows(rows,'faithfulness',2)
        self.assertEqual(r['coverage'],.5);self.assertEqual(r['meanOnScored'],.8);self.assertIsNone(r['fullSetMean'])

    def test_ragas_partial_judge_is_reported_separately(self):
        rows=[{'sampleId':'a','metric':'context_relevance','status':'scored_partial_judge','value':1.0}]
        r=ragas_report.summarize_rows(rows,'context_relevance',1)
        self.assertEqual(r['partial'],1);self.assertIsNone(r['fullSetMean'])

    def test_ragas_duplicate_samples_cannot_inflate_coverage(self):
        rows=[{'sampleId':'a','metric':'context_recall','status':'scored','value':1.0}]*2
        with self.assertRaises(ValueError):ragas_report.summarize_rows(rows,'context_recall',2)
