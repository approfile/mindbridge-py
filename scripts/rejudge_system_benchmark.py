"""Regrade immutable synthetic observations with the current grader in a new DB.

Keeps original judgments/reports intact so runtime comparisons can use the same
grader rather than confusing a grader repair with a runtime quality improvement.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--database',required=True)
    parser.add_argument('--sample-count',type=int,help='Developer-visible calibration subset only, not a full regrade')
    args=parser.parse_args()
    folder=args.output.resolve()
    if not folder.is_relative_to(ROOT/'target/system-benchmark') or folder.exists():raise ValueError('Use a fresh target/system-benchmark output directory')
    if not re.fullmatch(r'mindbridge_bench_[A-Za-z0-9_]+',args.database):raise ValueError('Dedicated database required')
    from app.core.config import Settings,get_settings
    from sqlalchemy.engine import make_url
    settings=Settings();url=make_url(settings.database_url)
    if args.database==url.database:raise ValueError('Do not reuse the business database')
    os.environ['DATABASE_URL']=url.set(database=args.database).render_as_string(hide_password=False)
    get_settings.cache_clear()
    from app.core.database import Base,engine,SessionLocal
    from app.models import entities
    from sqlalchemy import inspect
    if inspect(engine).get_table_names():raise ValueError('Empty database required')
    Base.metadata.create_all(engine)
    folder.mkdir(parents=True);(folder/'private').mkdir(mode=0o700)
    from app.benchmark.protocol import load_dataset,combine_contract,verdict_rate
    dataset,digest=load_dataset()
    plan=json.loads((args.input/'plan.json').read_text(encoding='utf-8'))
    if plan['datasetSha256']!=digest:raise ValueError('Dataset changed')
    source=args.input/'private/observations.json'
    observations=json.loads(source.read_text(encoding='utf-8'))
    if args.sample_count is not None:
        if args.sample_count<1:raise ValueError('Positive calibration count required')
        observations=[r for r in observations if r['layer']!='admission_probe'][:args.sample_count]
    from app.benchmark.judge import judge_observations,GRADER_VERSION
    from app.benchmark.runner import save
    from app.services.model_gateway import aggregate_usage
    save(folder/'plan.json',{'sourceObservations':str(source),'sourceSha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                             'datasetSha256':digest,'graderVersion':GRADER_VERSION,'calibrationSubset':args.sample_count is not None,
                             'plannedSamples':[x['sampleId'] for x in observations if x['layer']!='admission_probe']})
    judged=judge_observations(observations,dataset,folder,SessionLocal,settings)
    lookup={j['sampleId']:j for j in judged};groups=defaultdict(list)
    for item in observations:
        if item['layer']=='admission_probe':continue
        judge=lookup[item['sampleId']]
        decisions=[(judge['decision'] or {}).get(k,{}).get('verdict','unknown') for k in judge['requiredDimensions']]
        mechanical=all(all(t['mechanicalChecks'].values()) for t in item['turns'])
        key=f'{item["variant"]}/c{item["concurrency"]}/{item["layer"]}'
        groups[key].append(combine_contract(mechanical,decisions))
    costs=aggregate_usage([a for j in judged for a in j['usage']['attempts']],[])
    report={'graderVersion':GRADER_VERSION,'calibrationSubset':args.sample_count is not None,'sourceObservationsSha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'evaluations':len(judged),'valid':sum(j['decision'] is not None for j in judged),
            'invalidAttempts':sum(not a['valid'] for j in judged for a in j['attempts']),
            'groups':{k:verdict_rate(v) for k,v in groups.items()},'offlineJudgeUsage':costs}
    save(folder/'report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'groups','offlineJudgeUsage'}}),flush=True)


if __name__=='__main__':main()
