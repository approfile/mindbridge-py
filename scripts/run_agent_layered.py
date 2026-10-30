"""Preregister and run separated controlled / configured / intervention layers."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def load_tasks():
    spec=importlib.util.spec_from_file_location('metric_loader',ROOT/'scripts/score_agent_metrics.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m.load_tasks()


def child(args,tasks):
    task=next(t for t in tasks if t['id']==args.case_worker)
    try:
        if args.mode=='configured_model':
            from app.evaluation.agent_layered_execution import execute_configured
            obs,raw=execute_configured(task,args.state,args.nonce,args.trial)
        else:
            from app.evaluation.agent_execution import execute_task
            obs,raw=execute_task(task,args.state,args.nonce,args.trial)
        (args.state/'observation.json').write_text(json.dumps(obs,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        (args.state/'raw-evidence.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2,allow_nan=False)+'\n');os.chmod(args.state/'raw-evidence.json',0o600)
        return 0
    except Exception as e:
        import traceback
        args.state.mkdir(parents=True,exist_ok=True)
        (args.state/'error.json').write_text(json.dumps({'errorType':type(e).__name__,
          'frames':[{'file':f.filename,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(e.__traceback__)]},indent=2)+'\n')
        print(type(e).__name__,flush=True);return 1


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path)
    p.add_argument('--smoke',action='store_true');p.add_argument('--workers',type=int,default=2)
    p.add_argument('--layer',choices=['all','real','controlled'],default='all')
    p.add_argument('--case-worker');p.add_argument('--mode');p.add_argument('--state',type=Path);p.add_argument('--nonce');p.add_argument('--trial',default='trial-1')
    a=p.parse_args();tasks,digest=load_tasks()
    if a.case_worker:return child(a,tasks)
    if not a.output or not 1<=a.workers<=2:raise ValueError('Choose output and 1-2 workers')
    output=a.output.resolve()
    if not output.is_relative_to(ROOT/'target/agent-layered'):raise ValueError('Output must stay in target/agent-layered')
    output.mkdir(parents=True,exist_ok=False)
    nonce=uuid.uuid4().hex[:12];private=ROOT/'.local-runtime/agent-results-20261027'/('layered-'+nonce);private.mkdir();os.chmod(private,0o700)
    repeats={'normal-04','normal-08','high-risk-01','high-risk-04','memory-02','evidence-01'}
    planned=[]
    for t in tasks:
        faults=t['environment']['faults'];scope=t['executionScope'];context_only=bool(faults) and all(f['kind']=='replace_retrieval' for f in faults)
        if scope!='conversation' or faults:
            if a.layer in {'all','controlled'}:planned.append({'taskId':t['id'],'trialId':'trial-1','mode':'controlled_model' if scope=='conversation' else 'boundary_controlled','source':'evaluation_collector','layer':'controlled'})
        if scope=='conversation' and (not faults or context_only) and a.layer in {'all','real'}:
            for n in range(1,(3 if t['id'] in repeats and not faults else 1)+1):
                planned.append({'taskId':t['id'],'trialId':f'trial-{n}','mode':'configured_model','source':'evaluation_collector',
                   'layer':'real-context-intervention' if faults else 'real-nominal'})
    if a.smoke:planned=[x for x in planned if x['taskId']=='normal-04' and x['trialId']=='trial-1']
    sourcefiles=['app/evaluation/agent_execution.py','app/evaluation/agent_layered_execution.py','app/evaluation/agent_result_evidence.py','scripts/run_agent_layered.py']
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sourcefiles}
    manifest={'createdDate':'2026-10-27','datasetSha256':digest,'plannedTrials':planned,'sourceHashes':hashes,
      'privateEvidenceDirectory':str(private),'humanReviewPending':True,'judgeSameProviderBias':True,
      'repeatPolicy':'six preregistered development scenarios x3; others x1; never select best trial','cases':[]}
    (output/'plan.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    def execute(entry):
        key=entry['layer']+'-'+entry['taskId']+'-'+entry['trialId'];state=private/key
        cmd=[sys.executable,__file__,'--case-worker',entry['taskId'],'--mode',entry['mode'],'--state',str(state),'--nonce',nonce+'-'+key,'--trial',entry['trialId']]
        with (private/(key+'.log')).open('w') as log:
            try:code=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=650).returncode
            except subprocess.TimeoutExpired:code=124
        return entry,state,code
    observed={};count=0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for future in as_completed([pool.submit(execute,x) for x in planned]):
            entry,state,code=future.result();case={**entry,'exitCode':code,'state':str(state.relative_to(ROOT))}
            if (state/'observation.json').exists():observed[(entry['layer'],entry['taskId'],entry['trialId'])]=json.loads((state/'observation.json').read_text());case['captured']=True
            else:case['captured']=False
            manifest['cases'].append(case);count+=1
            (output/'execution-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
            for layer in {x['layer'] for x in planned}:
                folder=output/layer;folder.mkdir(exist_ok=True)
                payload={'schemaVersion':1,'runPlan':[{k:v for k,v in x.items() if k!='layer'} for x in planned if x['layer']==layer],
                    'observations':[v for (l,t,n),v in observed.items() if l==layer]}
                (folder/'observations.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
            print(f'{count}/{len(planned)} {entry["layer"]} {entry["taskId"]} {entry["trialId"]}: '+('captured' if case['captured'] else f'collector-error-{code}'),flush=True)
    if any(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=h for n,h in hashes.items()):raise RuntimeError('Execution source changed during run')
    from app.evaluation.agent_metrics import score_run
    for layer in {x['layer'] for x in planned}:
        folder=output/layer;payload=json.loads((folder/'observations.json').read_text())
        (folder/'mechanical-report.json').write_text(json.dumps(score_run(tasks,payload,digest),ensure_ascii=False,indent=2)+'\n')
    print('FINISHED; layers kept separate; independent semantic judging not run by this command.',flush=True)
    return 0 if len(observed)==len(planned) else 1


if __name__=='__main__':raise SystemExit(main())
