"""Score existing normalized observations. Does not run an Agent or call models."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.evaluation.agent_metrics import score_run


def load_tasks():
    spec=importlib.util.spec_from_file_location('frozen_agent_tasks',ROOT/'scripts/validate_agent_tasks.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.validate_dataset()
    manifest=json.loads((ROOT/'app/evaluation/data/agent-metrics-v1/manifest.json').read_text())
    for name,expected in manifest['artifactHashes'].items():
        path=(ROOT/name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('Frozen metric contract hash mismatch: '+name)
    p=ROOT/'app/evaluation/data/agent-tasks-v1/tasks.json'
    if (hashlib.sha256(p.read_bytes()).hexdigest()!=manifest['taskDatasetSha256']
        or hashlib.sha256((p.parent/'manifest.json').read_bytes()).hexdigest()!=manifest['taskManifestSha256']):
        raise ValueError('Metric/task frozen version mismatch')
    return json.loads(p.read_text())['tasks'],hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        tasks,digest=load_tasks()
        result=score_run(tasks,json.loads(args.input.read_text()),digest)
    except (ValueError,KeyError,OSError) as error:
        print('INVALID: '+str(error),file=sys.stderr);return 1
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'execution':result['execution'],'plannedTrials':result['plannedTrials'],
      'receivedObservations':result['receivedObservations'],'missingObservations':result['missingObservations'],
      'cohorts':[{k:v for k,v in c.items() if k not in {'rates','efficiency','cost','byCategory'}} for c in result['cohorts']],
      'agentTasksExecutedByThisCommand':0,'modelApiRequestsByThisCommand':0},ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
