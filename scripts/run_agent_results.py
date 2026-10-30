"""Run frozen tasks in isolated children and collect physical results (step 3).

Controlled model only; no real-model quality or independent semantic judging.
Requires existing local Redis 16379 and Mailpit SMTP 1025/API 8025.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def frozen_tasks():
    spec=importlib.util.spec_from_file_location('frozen_metric_entry',ROOT/'scripts/score_agent_metrics.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.load_tasks()


def child(args,tasks):
    from app.evaluation.agent_execution import execute_task
    task=next(t for t in tasks if t['id']==args.case_worker)
    try:
        observation,raw=execute_task(task,args.state,args.nonce,args.trial_id)
        (args.state/'observation.json').write_text(json.dumps(observation,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        (args.state/'raw-evidence.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        os.chmod(args.state/'raw-evidence.json',0o600)
        print('CAPTURED '+task['id'],flush=True);return 0
    except Exception as error:
        args.state.mkdir(parents=True,exist_ok=True)
        (args.state/'collector-error.json').write_text(json.dumps({'errorType':type(error).__name__,
          'frames':[{'file':f.filename,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)]},indent=2)+'\n')
        print('COLLECTOR_FAILED '+task['id']+' '+type(error).__name__,flush=True);return 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split',choices=['development','holdout'],default='development')
    parser.add_argument('--task',action='append',default=[])
    parser.add_argument('--output',type=Path)
    parser.add_argument('--case-worker',help=argparse.SUPPRESS)
    parser.add_argument('--state',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--nonce',help=argparse.SUPPRESS)
    parser.add_argument('--trial-id',default='trial-1',help=argparse.SUPPRESS)
    args=parser.parse_args();tasks,digest=frozen_tasks()
    if args.case_worker:return child(args,tasks)
    chosen=[t for t in tasks if t['split']==args.split and (not args.task or t['id'] in args.task)]
    if not chosen or set(args.task)-{t['id'] for t in chosen}:raise ValueError('Task selection must match the explicit dataset split')
    nonce=uuid.uuid4().hex[:12]
    source_files=['app/evaluation/agent_execution.py','app/evaluation/agent_result_evidence.py','scripts/run_agent_results.py']
    source_hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_files}
    private=ROOT/'.local-runtime/agent-results-20261027'/('run-'+nonce);private.mkdir(parents=True,exist_ok=False);os.chmod(private,0o700)
    output=(args.output or ROOT/'target/agent-results'/('run-'+nonce)).resolve()
    if not output.is_relative_to(ROOT/'target/agent-results'):raise ValueError('Reports must stay in target/agent-results')
    output.mkdir(parents=True,exist_ok=False)
    plan=[{'taskId':t['id'],'trialId':'trial-1','mode':'controlled_model' if t['executionScope']=='conversation' else 'boundary_controlled',
           'source':'evaluation_collector'} for t in chosen]
    payload={'schemaVersion':1,'runPlan':plan,'observations':[]}
    path=output/'observations.json';path.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    manifest={'createdDate':'2026-10-27','taskDatasetSha256':digest,'split':args.split,'plannedTasks':[t['id'] for t in chosen],
      'executionMode':'controlled_model_or_boundary','modelProvider':'existing mock; no gold-fed model outputs',
      'embedding':'controlled lexical hash, not Qwen quality','sqlBackend':'sqlite',
      'physicalServices':['real loopback HTTP','isolated SQLite','Redis namespace','Chroma','Excel file','local Mailpit SMTP/API'],
      'privateEvidenceDirectory':str(private),'cases':[],'semanticReviewStatus':'pending_step_4'}
    manifest['collectorSourceHashesAtStart']=source_hashes
    for index,task in enumerate(chosen,1):
        state=private/task['id'];log=private/(task['id']+'.log')
        timeout=len(task['turns'] or [None])*(60+120+15)+45
        cmd=[sys.executable,str(Path(__file__).resolve()),'--case-worker',task['id'],'--state',str(state),
             '--nonce',nonce+'-'+task['id'],'--trial-id','trial-1']
        with log.open('w') as stream:
            try:
                p=subprocess.run(cmd,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
                code=p.returncode
            except subprocess.TimeoutExpired:code=124
        captured=state/'observation.json'
        case={'taskId':task['id'],'exitCode':code,'captured':captured.exists(),'privateState':str(state.relative_to(ROOT))}
        if captured.exists():
            payload['observations'].append(json.loads(captured.read_text()))
            case['observationSha256']=hashlib.sha256(captured.read_bytes()).hexdigest()
            case['rawEvidenceSha256']=hashlib.sha256((state/'raw-evidence.json').read_bytes()).hexdigest()
        manifest['cases'].append(case)
        path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        (output/'execution-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        print(f'{index}/{len(chosen)} {task["id"]}: '+('captured' if captured.exists() else f'collector_error exit={code}'),flush=True)
    from app.evaluation.agent_metrics import score_run
    manifest['collectorSourceUnchangedDuringRun']=all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h for name,h in source_hashes.items())
    (output/'execution-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    if not manifest['collectorSourceUnchangedDuringRun']:raise RuntimeError('Collector code changed during execution; observations retained without claiming a fixed-version run')
    result=score_run(tasks,payload,digest)
    (output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'output':str(output),'plannedTrials':len(plan),'captured':len(payload['observations']),
      'missing':result['missingObservations'],'invalid':result['invalidObservations'],
      'modelApiRequests':0,'semanticJudgingExecuted':False},ensure_ascii=False),flush=True)
    return 0 if len(payload['observations'])==len(plan) else 1


if __name__=='__main__':
    raise SystemExit(main())
