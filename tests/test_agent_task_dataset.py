"""Dataset integrity checks only; no API, database or application execution."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('agent_task_validation',ROOT/'scripts/validate_agent_tasks.py')
validation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


class AgentTaskDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=json.loads((validation.DIRECTORY/'tasks.json').read_text())
        cls.schema=json.loads((validation.DIRECTORY/'schema.json').read_text())

    def altered(self, task_id):
        data=copy.deepcopy(self.data)
        return data,next(t for t in data['tasks'] if t['id']==task_id)

    def test_frozen_artifacts_and_existing_datasets_verify(self):
        report=validation.validate_dataset()
        self.assertEqual(report['tasks'],60)
        self.assertEqual(report['splits'],{'development':40,'holdout':20})
        self.assertFalse(report['evaluationExecuted'])
        self.assertEqual(report['modelApiRequests'],0)

    def test_duplicate_task_identity_is_rejected(self):
        d=copy.deepcopy(self.data);d['tasks'][1]['id']=d['tasks'][0]['id']
        with self.assertRaisesRegex(ValueError,'Duplicate task ID'):
            validation.validate_contracts(d,self.schema)

    def test_family_cannot_cross_development_and_holdout(self):
        d,t=self.altered('normal-03');t['familyId']='normal-social'
        with self.assertRaisesRegex(ValueError,'family leakage'):
            validation.validate_contracts(d,self.schema)

    def test_low_risk_tool_contract_cannot_expect_alert(self):
        d,t=self.altered('normal-04')
        t['turns'][0]['expected']['requiredTools'][0]['kind']='ALERT_SEND'
        with self.assertRaises(ValueError):
            validation.validate_contracts(d,self.schema)

    def test_foreign_session_cannot_be_normal_success(self):
        d,t=self.altered('memory-06');t['turns'][0]['expected']['donePolicy']='required'
        with self.assertRaisesRegex(ValueError,'foreign session must reject'):
            validation.validate_contracts(d,self.schema)

    def test_fault_needs_observed_trigger_criterion(self):
        d,t=self.altered('recovery-01')
        t['successCriteria']=[c for c in t['successCriteria'] if c['kind']!='fault_trigger_observed']
        with self.assertRaisesRegex(ValueError,'unverified fault trigger'):
            validation.validate_contracts(d,self.schema)

    def test_boundary_probes_cannot_claim_real_model_trials(self):
        d,t=self.altered('boundary-01');t['eligibleModes']=['configured_model']
        with self.assertRaisesRegex(ValueError,'cannot claim real-model'):
            validation.validate_contracts(d,self.schema)

    def test_fallback_cannot_emit_success_done(self):
        d,t=self.altered('recovery-04');t['turns'][0]['expected']['donePolicy']='required'
        with self.assertRaisesRegex(ValueError,'must not signal done'):
            validation.validate_contracts(d,self.schema)

    def test_high_risk_cannot_drop_report_on_model_failure(self):
        d,t=self.altered('recovery-04');t['turns'][0]['expected']['reportPolicy']='forbidden'
        with self.assertRaisesRegex(ValueError,'HIGH cannot lose report'):
            validation.validate_contracts(d,self.schema)

    def test_data_cannot_pretend_to_contain_executed_scores(self):
        d=copy.deepcopy(self.data);d['results']={'accuracy':1.0}
        with self.assertRaisesRegex(ValueError,'Schema error'):
            validation.validate_contracts(d,self.schema)
