"""Synthetic evidence tests; not model/Agent benchmark results."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from app.evaluation.agent_metrics import (digest_observation, score_run, score_trial,
    validate_observation, rate, distribution, effects_check)

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'app/evaluation/data/agent-tasks-v1/tasks.json'
TASKS=json.loads(DATA.read_text())['tasks']
DIGEST=hashlib.sha256(DATA.read_bytes()).hexdigest()
TASK_BY_ID={t['id']:t for t in TASKS}


def bind_reviews(task, observation, *, kind='external_assertion'):
    ids=[c['id'] for c in task['successCriteria']]+task['hardConstraints']
    observation['reviews']=[{'checkId':cid,'verdict':'pass','graderKind':kind,
      'graderId':'synthetic-unit-assertion','graderVersion':'unit-v1',
      'subjectDigest':digest_observation(observation),'evidenceRefs':['synthetic-fixture:constructed-state']} for cid in ids]
    return observation


def make_observation(task, trial='trial-1'):
    actors={'student_a':'u-a','student_b':'u-b','admin_plain':'u-admin'}
    sessions={s['alias']:{'id':'session-'+s['alias'],'ownerId':actors[s['owner']]} for s in task['initialState']['sessions']}
    o={'schemaVersion':1,'taskId':task['id'],'trialId':trial,
       'mode':'controlled_model' if task['executionScope']=='conversation' else 'boundary_controlled',
       'datasetSha256':DIGEST,'producer':{'kind':'synthetic_fixture','id':'unit-fixture','version':'unit-v1'},
       'fixtureStatus':'verified','bindings':{'actors':actors,'sessions':sessions},
       'faults':[{'id':f['id'],'stage':f['stage'],'trigger':f['trigger'],'triggered':True} for f in task['environment']['faults']],
       'turns':[],'boundaryResults':None,'reviews':[]}
    for inp in task['turns']:
        e=inp['expected'];rid='report-'+inp['id'];run='run-'+inp['id']
        route={'intent':e['acceptedIntents'][0] if e['acceptedIntents'] else None,
               'risk':e['acceptedRisks'][0] if e['acceptedRisks'] else None}
        expected_tools=[] if e['reportPolicy']=='if_consult_or_risk' and route['intent']=='CHAT' else e['requiredTools']
        reports=[] if not expected_tools else [{'id':rid,'userId':actors[inp['actor']],
          'sessionId':sessions[inp['session']]['id'],'risk':route['risk'],'intent':route['intent']}]
        jobs=[{'id':'job-'+inp['id']+'-'+r['kind'],'reportId':rid,'kind':r['kind'],'state':r['terminalState'],
               'attempts':1,'maxAttempts':3,'runId':run} for r in expected_tools]
        sources={'EXCEL_REPORT':'excel.ledger','CASE_CREATE':'sql.risk_cases','ALERT_SEND':'mailpit'}
        effects=[{'kind':j['kind'],'reportId':rid,'source':sources[j['kind']],'count':1} for j in jobs if j['state']=='SUCCESS']
        audits=[{'id':'audit-'+j['id'],'jobId':j['id'],'reportId':rid,'kind':j['kind'],
                 'allowed':True,'status':'SUCCESS' if j['state']=='SUCCESS' else 'FAILED','runId':run} for j in jobs]
        sha=hashlib.sha256(('synthetic-output-'+inp['id']).encode()).hexdigest()
        normal=e['replyMode'] in {'reviewed_reply','reviewed_reply_or_safe_fallback'}
        events=[{'seq':1,'kind':'RISK_ASSESSED','runId':run},{'seq':2,'kind':'PLAN_ACCEPTED','runId':run},
                {'seq':3,'kind':'GENERATION_STARTED','runId':run}]
        if normal:
            events += [{'seq':4,'kind':'BODY_REVIEW','runId':run,'approved':True,'candidateSha256':sha},
              {'seq':5,'kind':'SSE_TOKEN','runId':run,'source':'model_candidate','candidateSha256':sha},
              {'seq':6,'kind':'SSE_DONE','runId':run}]
        else:
            events += [{'seq':4,'kind':'SSE_TOKEN','runId':run,'source':'fixed_template','candidateSha256':sha},
                       {'seq':5,'kind':'SSE_ERROR','runId':run}]
        turn={'turnId':inp['id'],'actorAlias':inp['actor'],'sessionAlias':inp['session'],'runId':run,
          'coverage':{k:'complete' for k in ['route','reports','jobs','effects','audits','events','reply','timings','usage','invocations','memory','context','degradations','deadLetters']},
          'route':route,'reports':reports,'jobs':jobs,'effects':effects,'audits':audits,'events':events,
          'reply':{'mode':'reviewed_reply' if normal else 'safe_fallback','sha256':sha,'persistedAccepted':normal},
          'planChecks':{'currentArtifactBindings':True},'backgroundWindowClosed':True,
          'timings':{'turnMs':100.0,'firstReviewedTextMs':80.0 if normal else None,'backgroundMs':10.0},
          'usage':{'logicalModelCalls':3,'apiAttempts':3,'embeddingRequests':0,'providerTokens':None,'cost':None},
          'memory':{},'context':{'evidenceMode':'retrieved_support'},'degradations':[],
          'deadLetters':[j['id'] for j in jobs if j['state']=='DEAD'],
          'invocations':[{'seq':2*i+1,'completedSeq':2*i+2,'jobId':j['id'],'kind':j['kind'],
             'argumentsDigest':hashlib.sha256(j['id'].encode()).hexdigest(),'origin':'workflow',
             'outcome':'SUCCESS' if j['state']=='SUCCESS' else 'FAILED'} for i,j in enumerate(jobs)]}
        o['turns'].append(turn)
    if task['executionScope']!='conversation':
        o['boundaryResults']=[{'actionId':a['id'],'denied':True,'sideEffectCount':0,'auditFound':True,'httpStatus':403,'planAccepted':False} for a in task['boundaryActions']]
    return bind_reviews(task,o)


class AgentMetricTests(unittest.TestCase):
    def score(self,task_id,observation=None):
        task=TASK_BY_ID[task_id]
        return score_trial(task,observation or make_observation(task),DIGEST)

    def test_complete_state_evidence_fixture_can_pass(self):
        r=self.score('normal-04')
        self.assertEqual(r['contractStatus'],'pass');self.assertEqual(r['verifiedGoalStatus'],'complete')

    def test_no_tools_is_not_one_hundred_percent_tool_success(self):
        r=self.score('normal-01')
        self.assertEqual(r['points']['tool_chain_success'],['not_applicable'])
        self.assertIsNone(rate(r['points']['tool_chain_success'])['value'])

    def test_done_and_pending_is_not_task_completion(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task)
        o['turns'][0]['jobs'][0]['state']='PENDING';o['turns'][0]['effects']=[]
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['contractStatus'],'fail');self.assertEqual(r['points']['tool_chain_success'],[False])

    def test_open_background_window_is_unknown_not_failed_or_successful(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);t=o['turns'][0]
        t['jobs'][0]['state']='RUNNING';t['effects']=[];t['audits']=[];t['backgroundWindowClosed']=False
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['contractStatus'],'unknown')

    def test_fallback_does_not_fulfil_a_normal_reply_goal(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);t=o['turns'][0]
        t['reply'].update(mode='safe_fallback',persistedAccepted=False)
        t['events']=[e for e in t['events'] if e['kind']!='SSE_DONE']
        for e in t['events']:
            if e['kind']=='SSE_TOKEN':e['source']='fixed_template'
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['contractStatus'],'fail');self.assertIsNone(r['verifiedGoalStatus'])

    def test_safely_handled_generation_failure_is_only_partial(self):
        r=self.score('recovery-04')
        self.assertEqual(r['contractStatus'],'pass');self.assertEqual(r['verifiedGoalStatus'],'partial')
        self.assertEqual(r['points']['feasible_goal_completion'],[])
        self.assertEqual(r['points']['bounded_failure_handling'],[True])

    def test_runtime_safety_cannot_grade_its_own_output(self):
        task=TASK_BY_ID['normal-01'];o=make_observation(task)
        for r in o['reviews']:
            if r['checkId']=='reply_safety_boundaries':r['graderKind']='runtime_gate'
        r=score_trial(task,o,DIGEST)
        self.assertEqual(r['contractStatus'],'unknown');self.assertIsNone(r['hardConstraints']['reply_safety_boundaries'])
        self.assertTrue(any(x['reason']=='RUNTIME_GATE_IS_NOT_INDEPENDENT' for x in r['reviewDiagnostics']))

    def test_stale_review_digest_cannot_approve_new_evidence(self):
        task=TASK_BY_ID['normal-01'];o=make_observation(task);o['turns'][0]['timings']['turnMs']=101
        self.assertEqual(score_trial(task,o,DIGEST)['contractStatus'],'unknown')

    def test_conflicting_independent_reviews_are_unknown(self):
        task=TASK_BY_ID['normal-01'];o=make_observation(task)
        row=copy.deepcopy(next(r for r in o['reviews'] if r['checkId']=='reply_safety_boundaries'))
        row.update(verdict='fail',graderId='second-unit-reviewer');o['reviews'].append(row)
        self.assertEqual(score_trial(task,o,DIGEST)['contractStatus'],'unknown')

    def test_actual_foreign_effect_is_a_critical_violation(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);o['turns'][0]['reports'][0]['userId']='u-b'
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['contractStatus'],'fail');self.assertIn('authorized_effects_only',r['criticalFindings'])
        self.assertEqual(r['points']['forbidden_effect_incidence'],[True])

    def test_wrong_risk_cannot_generate_its_own_tool_gold(self):
        task=TASK_BY_ID['high-risk-01'];o=make_observation(task);t=o['turns'][0]
        t['route'].update(intent='CONSULT',risk='LOW');t['reports'][0].update(intent='CONSULT',risk='LOW')
        t['jobs']=[t['jobs'][0]];t['effects']=[t['effects'][0]];t['audits']=[t['audits'][0]]
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['points']['route_correct'],[False]);self.assertEqual(r['points']['report_tool_match'],[False])

    def test_review_after_visible_text_fails_gate(self):
        task=TASK_BY_ID['normal-01'];o=make_observation(task)
        for e in o['turns'][0]['events']:
            if e['kind']=='BODY_REVIEW':e['seq']=7
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertFalse(r['hardConstraints']['no_unreviewed_candidate']);self.assertEqual(r['contractStatus'],'fail')

    def test_normal_tokens_after_deadline_fail_gate(self):
        task=TASK_BY_ID['normal-01'];o=make_observation(task);t=o['turns'][0]
        for e in t['events']:e['seq']*=2
        t['events'].append({'seq':9,'kind':'TURN_TIMEOUT','runId':t['runId']})
        self.assertEqual(score_trial(task,bind_reviews(task,o),DIGEST)['contractStatus'],'fail')

    def test_new_reports_in_distinct_turns_are_not_duplicate_effects(self):
        r=self.score('high-risk-10')
        self.assertEqual(r['contractStatus'],'pass');self.assertEqual(r['points']['duplicate_effect_incidence'],[False,False])

    def test_reusing_one_report_across_turns_fails_binding(self):
        task=TASK_BY_ID['high-risk-10'];o=make_observation(task)
        old=o['turns'][1]['reports'][0]['id'];new=o['turns'][0]['reports'][0]['id']
        t=o['turns'][1];t['reports'][0]['id']=new
        for k in ('jobs','effects','audits'):
            for r in t[k]:r['reportId']=new
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertFalse(r['hardConstraints']['current_turn_bindings'])

    def test_duplicate_effect_and_duplicate_call_are_distinct(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);t=o['turns'][0]
        call=copy.deepcopy(t['invocations'][0]);call.update(seq=3,completedSeq=4);t['invocations'].append(call)
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['points']['duplicate_effect_incidence'],[False])
        self.assertEqual(r['points']['redundant_after_success_turn_incidence'],[True])

    def test_retry_after_failure_is_not_redundant_after_success(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);t=o['turns'][0]
        call=copy.deepcopy(t['invocations'][0]);call.update(seq=3,completedSeq=4)
        t['invocations'][0]['outcome']='FAILED';t['invocations'].append(call);t['jobs'][0]['attempts']=2
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['points']['redundant_after_success_turn_incidence'],[False])

    def test_unknown_prior_tool_result_does_not_become_a_proven_duplicate(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);t=o['turns'][0]
        call=copy.deepcopy(t['invocations'][0]);call.update(seq=3,completedSeq=4);t['invocations'][0]['outcome']='UNKNOWN';t['invocations'].append(call)
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['points']['redundant_after_success_turn_incidence'],[None])

    def test_expected_dead_is_handled_but_not_a_successful_delivery(self):
        task=copy.deepcopy(TASK_BY_ID['high-risk-01']);task['expectedOutcome'].update(goalStatus='partial',handling='safe_degradation')
        next(r for r in task['turns'][0]['expected']['requiredTools'] if r['kind']=='ALERT_SEND')['terminalState']='DEAD'
        o=make_observation(task);t=o['turns'][0]
        self.assertTrue(effects_check(task['turns'][0]['expected'],t,o['bindings']))
        self.assertFalse(effects_check(task['turns'][0]['expected'],t,o['bindings'],require_success=True))

    def test_fault_not_triggered_cannot_earn_recovery_success(self):
        task=TASK_BY_ID['recovery-01'];o=make_observation(task);o['faults'][0]['triggered']=False
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertFalse(r['integrity']);self.assertEqual(r['contractStatus'],'unknown')

    def test_absent_provider_usage_and_price_remain_unknown(self):
        r=self.score('normal-04')
        self.assertEqual(r['efficiency']['totalTokens'],[None]);self.assertEqual(r['efficiency']['costs'],[None])

    def test_explicit_provider_usage_and_currency_are_preserved(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task)
        o['turns'][0]['usage'].update(providerTokens={'source':'provider_usage','complete':True,'prompt':10,'completion':5,'total':15},
           cost={'amount':0.01,'currency':'CNY','pricingVersion':'unit-price-v1'})
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['efficiency']['totalTokens'],[15]);self.assertEqual(r['efficiency']['costs'][0]['currency'],'CNY')

    def test_malformed_numeric_values_are_rejected(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);o['turns'][0]['timings']['turnMs']=float('nan')
        with self.assertRaisesRegex(ValueError,'Non-finite'):validate_observation(o)
        o=make_observation(task);o['turns'][0]['usage']['apiAttempts']=True
        with self.assertRaisesRegex(ValueError,'schema'):validate_observation(o)

    def test_missing_effect_coverage_is_not_an_empty_successful_world(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);o['turns'][0]['coverage']['effects']='unavailable'
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['contractStatus'],'unknown');self.assertEqual(r['points']['tool_chain_success'],[None])

    def test_boundary_denial_is_contract_pass_but_no_goal_completion_rate(self):
        r=self.score('boundary-03')
        self.assertEqual(r['contractStatus'],'pass');self.assertEqual(r['verifiedGoalStatus'],'blocked')
        self.assertEqual(r['points']['feasible_goal_completion'],[]);self.assertEqual(r['points']['forbidden_effect_incidence'],[False])

    def test_missing_planned_trial_stays_in_denominator(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task)
        p={'schemaVersion':1,'runPlan':[{'taskId':task['id'],'trialId':x,'mode':'controlled_model','source':'synthetic_fixture'} for x in ('trial-1','trial-2')], 'observations':[o]}
        r=score_run(TASKS,p,DIGEST);m=r['cohorts'][0]['rates']['contract_pass']
        self.assertEqual((m['numerator'],m['denominator'],m['unknown']),(1,2,1));self.assertIsNone(m['value'])
        self.assertEqual((m['lowerBound'],m['upperBound']),(.5,1.0));self.assertEqual(r['missingObservations'],1)
        self.assertEqual(r['cohorts'][0]['efficiency']['turnMs']['missing'],1)

    def test_invalid_observation_is_preserved_as_fixture_failure(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);o['turns'][0]['usage']['apiAttempts']='three'
        p={'schemaVersion':1,'runPlan':[{'taskId':task['id'],'trialId':'trial-1','mode':'controlled_model','source':'synthetic_fixture'}], 'observations':[o]}
        r=score_run(TASKS,p,DIGEST)
        self.assertFalse(r['trials'][0]['integrity']);self.assertEqual(r['trials'][0]['contractStatus'],'unknown')

    def test_duplicate_observation_cannot_inflate_trial_count(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task)
        p={'schemaVersion':1,'runPlan':[{'taskId':task['id'],'trialId':'trial-1','mode':'controlled_model','source':'synthetic_fixture'}], 'observations':[o,o]}
        with self.assertRaisesRegex(ValueError,'Duplicate observation'):score_run(TASKS,p,DIGEST)

    def test_efficiency_quantiles_use_declared_nearest_rank(self):
        d=distribution([10,20,30,None]);self.assertEqual(d['p50'],20);self.assertEqual(d['p95'],30);self.assertEqual(d['missing'],1)

    def test_zero_denominator_is_not_one(self):
        self.assertIsNone(rate([])['value']);self.assertIsNone(rate(['not_applicable'])['value'])

    def test_completed_tool_without_terminal_audit_cannot_pass(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task);o['turns'][0]['audits']=[]
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['contractStatus'],'fail');self.assertFalse(r['executionChecks']['t1.audit'])

    def test_alert_started_before_case_completion_fails_dependency(self):
        task=TASK_BY_ID['high-risk-01'];o=make_observation(task);t=o['turns'][0]
        next(c for c in t['invocations'] if c['kind']=='CASE_CREATE')['completedSeq']=10
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['points']['tool_dependency_order_correct'],[False]);self.assertEqual(r['contractStatus'],'fail')

    def test_case_final_state_without_completion_time_is_insufficient(self):
        task=TASK_BY_ID['high-risk-01'];o=make_observation(task)
        next(c for c in o['turns'][0]['invocations'] if c['kind']=='CASE_CREATE').pop('completedSeq')
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['points']['tool_dependency_order_correct'],[None]);self.assertEqual(r['contractStatus'],'unknown')

    def test_repeated_snapshot_is_not_a_second_business_effect(self):
        task=TASK_BY_ID['normal-04'];o=make_observation(task)
        o['turns'][0]['effects'].append(copy.deepcopy(o['turns'][0]['effects'][0]))
        with self.assertRaisesRegex(ValueError,'Duplicate aggregate effect'):validate_observation(o)

    def test_source_scope_and_execution_mode_are_never_pooled(self):
        a=TASK_BY_ID['normal-01'];b=TASK_BY_ID['boundary-03'];oa=make_observation(a);ob=make_observation(b)
        p={'schemaVersion':1,'runPlan':[
          {'taskId':a['id'],'trialId':'trial-1','mode':'controlled_model','source':'synthetic_fixture'},
          {'taskId':b['id'],'trialId':'trial-1','mode':'boundary_controlled','source':'synthetic_fixture'}], 'observations':[oa,ob]}
        result=score_run(TASKS,p,DIGEST)
        self.assertEqual(len(result['cohorts']),2)
        self.assertEqual({c['executionScope'] for c in result['cohorts']},{'conversation','tool_boundary'})

    def test_token_mismatch_cannot_be_reported_as_measured_usage(self):
        task=TASK_BY_ID['normal-01'];o=make_observation(task)
        o['turns'][0]['usage']['providerTokens']={'source':'provider_usage','complete':True,'prompt':10,'completion':5,'total':100}
        with self.assertRaisesRegex(ValueError,'Inconsistent normalized provider'):validate_observation(o)

    def test_invalid_fault_trial_preserves_observed_critical_findings(self):
        task=TASK_BY_ID['recovery-01'];o=make_observation(task);t=o['turns'][0]
        o['faults'][0]['triggered']=False;t['reports'][0]['userId']='u-b'
        r=score_trial(task,bind_reviews(task,o),DIGEST)
        self.assertEqual(r['contractStatus'],'unknown');self.assertIn('authorized_effects_only',r['criticalFindings'])

    def test_provider_costs_in_different_currencies_stay_separate(self):
        task=TASK_BY_ID['normal-04'];observations=[]
        for trial,currency in [('t-cny','CNY'),('t-usd','USD')]:
            o=make_observation(task,trial);o['turns'][0]['usage'].update(
              providerTokens={'source':'provider_usage','complete':True,'prompt':2,'completion':1,'total':3},
              cost={'amount':.01,'currency':currency,'pricingVersion':'unit-v1'})
            observations.append(bind_reviews(task,o))
        p={'schemaVersion':1,'runPlan':[{'taskId':task['id'],'trialId':o['trialId'],'mode':'controlled_model','source':'synthetic_fixture'} for o in observations], 'observations':observations}
        result=score_run(TASKS,p,DIGEST)
        self.assertEqual(set(result['cohorts'][0]['cost']['byCurrency']),{'CNY','USD'})
        self.assertEqual(result['uniquePlannedTasks'],1)
