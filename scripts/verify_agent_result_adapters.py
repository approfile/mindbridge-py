"""Independent SMTP adapter probes; not part of the 60 frozen benchmark tasks."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',choices=['transient','permanent','disconnect','http-denial'])
    parser.add_argument('--nonce');parser.add_argument('--state',type=Path)
    args=parser.parse_args()
    if args.worker:
        from app.evaluation.agent_execution import execute_task
        task=copy.deepcopy(next(t for t in json.loads((ROOT/'app/evaluation/data/agent-tasks-v1/tasks.json').read_text())['tasks'] if t['id']=='high-risk-01'))
        task['id']='adapter-probe-'+args.worker
        task['environment']['configurationOverrides']={'tool_queue_retry_delay_seconds':.1,'tool_queue_max_attempts':3}
        task['environment']['faults']=[{'id':'adapter-smtp','kind':'raise_before_side_effect','stage':'tool.ALERT_SEND',
          'trigger':'authorized_send_before_side_effect','parameters':{'times':1 if args.worker=='transient' else 'all'},
          'mustObserveTrigger':True,'adapterStatus':'integration_probe'}]
        if args.worker=='disconnect':
            task['environment']['faults']=[{'id':'adapter-disconnect','kind':'client_disconnect','stage':'http.sse',
              'trigger':'after_report_outbox_committed_before_candidate_exposure','parameters':{'times':1},
              'mustObserveTrigger':True,'adapterStatus':'integration_probe'}]
        if args.worker=='http-denial':
            task['executionScope']='http_boundary';task['turns']=[];task['environment']['faults']=[]
            task['initialState']['reports']=[{'alias':'protected_probe','session':'main','risk':'HIGH','caseStatus':'OPEN'}]
            task['boundaryActions']=[{'id':'student-admin','kind':'http_request','actor':'student_a',
               'target':'GET /api/admin/reports/{reportRef}','arguments':{'reportRef':'protected_probe'}},
              {'id':'admin-sensitive','kind':'http_request','actor':'admin_plain',
               'target':'GET /api/admin/reports/{reportRef}?includeSensitive=true','arguments':{'reportRef':'protected_probe','accessPurpose':'incident_review'}}]
        obs,raw=execute_task(task,args.state,args.nonce,'adapter-trial')
        if args.worker=='http-denial':
            assert len(obs['boundaryResults'])==2
            assert all(x['denied'] and x['httpStatus']==403 and x['sideEffectCount']==0 for x in obs['boundaryResults'])
            assert next(x for x in obs['reviews'] if x['checkId']=='scope_ownership')['verdict']=='pass'
            result={'scope':'adapter_probe_not_benchmark','type':args.worker,'verified':True,
                    'actualHttpResults':obs['boundaryResults'],'modelApiRequests':0}
            (args.state/'raw-evidence.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n')
            (args.state/'probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');return
        turn=obs['turns'][0];alert=next(j for j in turn['jobs'] if j['kind']=='ALERT_SEND')
        receipts=sum(e['count'] for e in turn['effects'] if e['kind']=='ALERT_SEND')
        expected_state='DEAD' if args.worker=='permanent' else 'SUCCESS'
        assert alert['state']==expected_state and alert['attempts']==({'transient':2,'permanent':3,'disconnect':1}[args.worker])
        assert receipts==(0 if args.worker=='permanent' else 1)
        assert all(j['state']=='SUCCESS' for j in turn['jobs'] if j['kind']!='ALERT_SEND')
        assert obs['faults'][0]['triggered']
        if args.worker=='permanent':assert alert['id'] in turn['deadLetters']
        if args.worker=='disconnect':
            assert any(e['kind']=='CLIENT_DISCONNECTED' for e in turn['events'])
            assert not any(e['kind'] in {'GENERATION_STARTED','SSE_DONE'} for e in turn['events'])
        (args.state/'raw-evidence.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n')
        (args.state/'probe.json').write_text(json.dumps({'scope':'adapter_probe_not_benchmark','type':args.worker,'verified':True,
          'alertState':alert['state'],'attempts':alert['attempts'],'actualMailpitMessages':receipts,
          'otherJobsSucceeded':True,'actualEffects':turn['effects'],'modelProvider':'controlled_mock','modelApiRequests':0},ensure_ascii=False,indent=2)+'\n')
        return
    nonce=uuid.uuid4().hex[:12];folder=ROOT/'.local-runtime/agent-results-20261027'/('adapter-'+nonce);folder.mkdir();os.chmod(folder,0o700)
    results=[]
    for variant in ['transient','permanent','disconnect','http-denial']:
        state=folder/variant
        with (folder/(variant+'.log')).open('w') as log:
            p=subprocess.run([sys.executable,__file__,'--worker',variant,'--state',str(state),'--nonce',nonce+'-'+variant],cwd=ROOT,
               stdout=log,stderr=subprocess.STDOUT,timeout=120)
        if p.returncode:raise RuntimeError('Adapter probe failed: '+variant+'; see '+str(folder))
        results.append(json.loads((state/'probe.json').read_text()))
    output=ROOT/'target/agent-results/adapter-probes';output.mkdir(parents=True,exist_ok=True)
    (output/'report.json').write_text(json.dumps({'probes':results,'privateEvidenceDirectory':str(folder),
      'partOfFrozenBenchmark':False,'preservedHoldout':True},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
