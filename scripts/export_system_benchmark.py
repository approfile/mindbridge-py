"""Audit frozen artifacts and export a compact public summary, without API calls.

Adds observed session/cache strata; requested repeat labels never prove that a
session actually continued when the first turn failed. Raw artifacts stay intact.
"""
import argparse
from collections import defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.benchmark.protocol import load_dataset, subject_digest, verdict_rate
from app.services.model_gateway import aggregate_usage


def actual_label(case, turns, index):
    if index == 0:
        return 'seeded_history_first_read' if case.get('seedHistoryPairs') else 'initial_new_session'
    previous, current = turns[index-1], turns[index]
    if previous.get('sessionId') and current.get('sessionId') == previous['sessionId']:
        return 'same_session_repeat'
    return 'continuation_not_established'


def export(folder, destination):
    plan = json.loads((folder/'plan.json').read_text(encoding='utf-8'))
    report = json.loads((folder/'report.json').read_text(encoding='utf-8'))
    observations = json.loads((folder/'private/observations.json').read_text(encoding='utf-8'))
    judgments = json.loads((folder/'private/judgments.json').read_text(encoding='utf-8'))
    data, digest = load_dataset()
    if plan['datasetSha256'] != digest or report['datasetSha256'] != digest:
        raise ValueError('Dataset provenance mismatch')
    expected = {row['sampleId'] for row in plan['plannedTrials']}
    actual = {row['sampleId'] for row in observations}
    if expected != actual or len(observations)!=len(expected):
        raise ValueError('Missing or duplicate planned scenarios')
    cases = {row['id']:row for row in data['nominal']+data['controlled']}
    lookup = {row['sampleId']:row for row in observations}
    for judge in judgments:
        row = lookup[judge['sampleId']]
        if judge['subjectDigest'] != subject_digest(cases[row['caseId']],row['turns']):
            raise ValueError('Judge evidence mismatch')
    # Derive final verdicts exactly from the published grading rule.
    from app.benchmark.protocol import combine_contract
    judged = {row['sampleId']:row for row in judgments}
    rows, strata, categories = [], defaultdict(list), defaultdict(list)
    for item in observations:
        if item['layer']=='admission_probe': continue
        case = cases[item['caseId']]
        judgment = judged.get(item['sampleId'],{})
        verdicts = [(judgment.get('decision') or {}).get(k,{}).get('verdict','unknown') for k in judgment.get('requiredDimensions',['goal','safety'])]
        passed = all(all(t['mechanicalChecks'].values()) for t in item['turns'])
        verdict = combine_contract(passed,verdicts)
        categories[(item['variant'],case['category'])].append(verdict)
        for index,turn in enumerate(item['turns']):
            label = actual_label(case,item['turns'],index)
            if item['layer'] in {'nominal','context_intervention'}:
                strata[(item['variant'],label)].append(turn)
            rows.append({'sampleId':item['sampleId'],'variant':item['variant'],'concurrency':item['concurrency'],
                         'layer':item['layer'],'caseId':item['caseId'],'trial':item['trial'],'turnIndex':index,
                         'plannedLabel':turn['observationLabel'],'observedLabel':label,'contract':verdict,
                         'requestId':turn['requestId'],'httpStatus':turn['httpStatus'],
                         'clientMs':turn['clientEndToEndMs'], 'serverState':(turn.get('usage') or {}).get('streamStatus','no_ledger')})
    cache = {}
    for key, turns in sorted(strata.items()):
        usage = aggregate_usage([a for t in turns if t.get('usage') for a in t['usage']['attempts']],
                                [e for t in turns if t.get('usage') for e in t['usage']['cacheEvents']])
        cache['/'.join(key)] = {field:usage[field] for field in ['providerCacheHitRate','cacheTokenCoverage','cacheLayers']}
        cache['/'.join(key)]['httpRequests'] = len(turns)
    captured_attempts = {a['id'] for row in observations for turn in row['turns'] if turn.get('usage') for a in turn['usage']['attempts']}
    with (folder/'attempts.csv').open(encoding='utf-8-sig',newline='') as handle:
        ledger_attempts = {row['id'] for row in csv.DictReader(handle) if row['scope']=='online_chat'}
    if captured_attempts != ledger_attempts:
        raise ValueError('Online attempts do not reconcile with scoped ledger CSV')
    current_sources_match = all((ROOT/path).exists() and hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==checksum
                                for path,checksum in plan['sourceHashes'].items())
    artifact_names = ['plan.json','report.json','turns.csv','attempts.csv','private/observations.json','private/judgments.json']
    summary = {'protocol':report['protocol'],'createdAt':plan['createdAt'],'datasetVersion':plan['datasetVersion'],
               'datasetSha256':digest,'smoke':plan['smoke'],'mock':plan['mock'],
               'plannedScenarios':len(expected),'capturedScenarios':len(observations),
               'capturedHttpRequests':sum(len(row['turns']) for row in observations),
               'requestIdsUnique':all(t['requestId'] for r in observations for t in r['turns']) and len({t['requestId'] for r in observations for t in r['turns']})==sum(len(r['turns']) for r in observations),
               'onlineAttemptLedgerReconciled':True,'measurementSourcesStillMatch':current_sources_match,
               'groups':report['groups'],'cohortPerformance':report['cohortPerformance'],
               'costScopes':report['costScopes'],'admission':report['admission'],'physicalEvidence':report['physicalEvidence'],
               'cacheByObservedSessionState':cache,
               'contractsByCategory':{'/'.join(key):verdict_rate(values) for key,values in sorted(categories.items())},
               'judgeEvaluations':len(judgments),'invalidJudgeAttempts':sum(not a['valid'] for row in judgments for a in row['attempts']),
               'judgeCoverage':sum(row.get('decision') is not None for row in judgments)/len(judgments) if judgments else None,
               'artifactHashes':{name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in artifact_names},
               'limitations':report['limitations'],
               'annotationVersion':'observed-session-audit-v1; planned repeat labels checked against actual session IDs; raw files unchanged'}
    output = io.StringIO(newline=''); writer = csv.DictWriter(output,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    (folder/'audited-turns.csv').write_text(output.getvalue(),encoding='utf-8-sig')
    snapshot = folder/'source-snapshot.zip'
    if current_sources_match and not snapshot.exists():
        with zipfile.ZipFile(snapshot,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name in plan['sourceHashes']:
                archive.write(ROOT/name,name)
            for path in sorted((ROOT/'app/knowledge').glob('*.md')):
                archive.write(path,path.relative_to(ROOT))
            for path in sorted(ROOT.glob('requirements*.txt')):
                archive.write(path,path.name)
            archive.write(ROOT/'app/benchmark/dataset-v1.sha256','app/benchmark/dataset-v1.sha256')
    for name in ['audited-turns.csv','source-snapshot.zip']:
        if (folder/name).exists(): summary['artifactHashes'][name] = hashlib.sha256((folder/name).read_bytes()).hexdigest()
    summary['exportToolSha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('AUDITED '+str(destination))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    export(args.input,args.output)
