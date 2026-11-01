import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.benchmark.protocol import (combine_contract, cost_shares, latency, load_dataset,
                                     subject_digest, validate_judgment, verdict_rate)
from app.benchmark.report import summarize


class BenchmarkProtocolTests(unittest.TestCase):
    def test_frozen_dataset_covers_required_families(self):
        dataset, digest = load_dataset()
        self.assertEqual(len(digest),64)
        self.assertTrue({'CHAT','CONSULT','RISK','LONG_HISTORY','INSUFFICIENT_EVIDENCE','REPEATED_SESSION'} <= {row['category'] for row in dataset['nominal']})
        self.assertEqual({row['fault'] for row in dataset['controlled']},{'first_stream_503','all_stream_503'})
        self.assertEqual(len(dataset['nominal']),8)

    def test_changed_dataset_is_rejected_instead_of_silently_relabelled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'data.json'; path.write_text('{"changed":true}')
            path.with_suffix('.sha256').write_text('0'*64)
            with patch('app.benchmark.protocol.DATASET',path), self.assertRaises(ValueError): load_dataset()

    def test_nearest_rank_percentiles_and_missing_samples(self):
        value = latency([1,2,3,4,None])
        self.assertEqual(value,{'samples':4,'p50Ms':2,'p95Ms':4})
        self.assertEqual(latency([None,float('nan')])['samples'],0)
        self.assertIsNone(latency([])['p95Ms'])

    def test_unknown_judgments_stay_in_denominator_with_bounds(self):
        value = verdict_rate(['pass','fail','unknown','unknown'])
        self.assertIsNone(value['rate'])
        self.assertEqual((value['lowerBound'],value['upperBound']),(.25,.75))
        self.assertEqual(value['unknown'],2)

    def test_semantic_pass_cannot_override_mechanical_failure(self):
        self.assertEqual(combine_contract(False,['pass','pass']),'fail')
        self.assertEqual(combine_contract(True,['pass','unknown']),'unknown')
        self.assertEqual(combine_contract(True,[]),'unknown')
        self.assertEqual(combine_contract(True,['pass','pass']),'pass')

    def test_judge_quote_must_come_from_actual_reply(self):
        value = {'goal':{'verdict':'pass','reason':'quoted','quotes':['invented reply']}}
        with self.assertRaises(ValueError): validate_judgment(value,'actual reply',['goal'])
        value['goal']['quotes'] = ['actual reply']
        self.assertEqual(validate_judgment(value,'actual reply',['goal']),value)

    def test_judge_missing_dimensions_and_empty_evidence_are_invalid(self):
        with self.assertRaises(ValueError): validate_judgment({},'reply',['goal'])
        with self.assertRaises(ValueError):
            validate_judgment({'goal':{'verdict':'pass','reason':'okay','quotes':[]}},'reply',['goal'])

    def test_judgment_digest_binds_answer_context_and_contract(self):
        case = {'rubric':'do A'}
        turns = [{'requestId':'id','visibleText':'reply','retrievedContexts':['evidence'],'mechanicalChecks':{'ok':True}}]
        first = subject_digest(case,turns)
        turns[0]['visibleText'] = 'changed'
        self.assertNotEqual(first,subject_digest(case,turns))
        self.assertNotEqual(subject_digest(case,turns),subject_digest({'rubric':'do B'},turns))

    def test_agent_cost_shares_keep_currencies_separate_and_omit_unknown(self):
        attempts = [{'agent':'A','cost':{'status':'priced','currency':'USD','amount':'1'}},
                    {'agent':'B','cost':{'status':'priced','currency':'USD','amount':'3'}},
                    {'agent':'A','cost':{'status':'priced','currency':'CNY','amount':'2'}},
                    {'agent':'B','cost':{'status':'usage_unknown','amount':None}}]
        shares = cost_shares(attempts)
        self.assertEqual(shares['A']['USD']['shareOfKnownCost'],.25)
        self.assertEqual(shares['A']['CNY']['shareOfKnownCost'],1)

    def test_environment_refuses_business_db_and_scopes_all_storage(self):
        from app.benchmark.runner import isolated_environment
        from app.core.config import Settings, get_settings
        settings = Settings(_env_file=None,database_url='mysql+pymysql://fixture@mysql/mindbridge',ai_provider='mock')
        with patch.dict(os.environ):
            with self.assertRaises(ValueError): isolated_environment('mindbridge',Path('/tmp/run'),'event_driven',True,settings)
            env = isolated_environment('mindbridge_bench_test',Path('/tmp/benchmark-run'),'event_driven',True,settings)
            self.assertIn('/mindbridge_bench_test',env['DATABASE_URL'])
            self.assertEqual(env['REDIS_URL'],'redis://redis:6379/14')
            self.assertTrue(env['BENCHMARK_NAMESPACE'].startswith('mb-bench:benchmark-run:'))
            self.assertEqual(env['SMTP_HOST'],'mailpit')
            self.assertIn('/private/',env['PRIVACY_KEY_FILE'])
            second = isolated_environment('mindbridge_bench_test',Path('/tmp/benchmark-run'),'sequential',True,settings)
            self.assertNotEqual(env['BENCHMARK_NAMESPACE'],second['BENCHMARK_NAMESPACE'])
        get_settings.cache_clear()

    def test_throughput_uses_group_makespan_and_keeps_failure_latency(self):
        turns = []
        for start,end,done in [(10,12,True),(10,14,False)]:
            turns.append({'clientStart':start,'clientEnd':end,'clientEndToEndMs':(end-start)*1000,
                          'clientFirstVisibleTokenMs':None,'httpStatus':200,'sseEvents':['done'] if done else ['error'],
                          'usage':None,'mechanicalChecks':{'ok':done}})
        item = {'turns':turns,'contractVerdict':'unknown','judgeVerdicts':{},'requiredDimensions':['goal']}
        value = summarize([item])
        self.assertEqual(value['observedWallSeconds'],4)
        self.assertEqual(value['completedReplyThroughput'],.25)
        self.assertEqual(value['httpThroughput'],.5)
        self.assertEqual(value['clientEndToEnd']['p95Ms'],4000)
        self.assertIsNone(value['tokens'])
        self.assertEqual(value['taskContract']['unknown'],1)

    def test_private_runtime_probe_is_reachable_before_root_static_mount(self):
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from fastapi.testclient import TestClient
        from types import SimpleNamespace
        from app.benchmark.server import add_runtime_route
        with tempfile.TemporaryDirectory() as folder:
            app = FastAPI()
            app.state.turn_runtime = SimpleNamespace(stats=lambda:{'active':0,'limit':4})
            app.mount('/',StaticFiles(directory=folder,html=True))
            add_runtime_route(app)
            with TestClient(app) as client:
                response = client.get('/__benchmark/runtime')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['active'],0)

    def test_database_diagnostics_keep_errno_without_sql_or_parameters(self):
        from sqlalchemy.exc import OperationalError
        from app.benchmark.server import _CASE, record_database_error
        class DriverError(Exception): pass
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,BENCHMARK_OUTPUT=folder):
            (Path(folder)/'private').mkdir()
            context = {'sampleTraceId':'a'*32,'diagnostics':[]}
            token = _CASE.set(context)
            try:
                record_database_error(OperationalError('PRIVATE_SQL',{'secret':'PRIVATE_SECRET'},DriverError(1213,'PRIVATE_ERROR')),'memory.synchronize')
            finally: _CASE.reset(token)
            text = (Path(folder)/'private/diagnostics'/('a'*32+'.json')).read_text()
            self.assertIn('1213',text)
            self.assertNotIn('PRIVATE',text)

    def test_repeat_cache_label_requires_an_actual_continued_session(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('benchmark_export',Path(__file__).resolve().parents[1]/'scripts/export_system_benchmark.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertEqual(module.actual_label({},[{}, {'sessionId':'new'}],1),'continuation_not_established')
        self.assertEqual(module.actual_label({},[{'sessionId':'same'}, {'sessionId':'same'}],1),'same_session_repeat')
        self.assertEqual(module.actual_label({'seedHistoryPairs':12},[{}],0),'seeded_history_first_read')


if __name__ == '__main__': unittest.main()
