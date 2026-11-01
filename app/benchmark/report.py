"""Rebuild public aggregates and CSV from private frozen observations."""
from collections import defaultdict
import csv
import hashlib
import io
import json

from app.benchmark.protocol import combine_contract, cost_shares, latency, subject_digest, verdict_rate


def identity(item):
    return item.get('sampleId') or tuple(item[k] for k in ['variant','concurrency','trial','caseId','layer'])


def summarize(items):
    from app.services.model_gateway import aggregate_usage
    turns = [turn for item in items for turn in item['turns']]
    attempts = [attempt for turn in turns if turn.get('usage') for attempt in turn['usage']['attempts']]
    events = [event for turn in turns if turn.get('usage') for event in turn['usage']['cacheEvents']]
    summary = aggregate_usage(attempts, events)
    start = min((row['clientStart'] for row in turns), default=None)
    end = max((row['clientEnd'] for row in turns), default=None)
    wall = end-start if start is not None else None
    completed = sum('done' in row['sseEvents'] for row in turns)
    summary.update(scenarios=len(items), httpRequests=len(turns), sseDone=completed,
                   missingLedgerRequests=sum(row.get('usage') is None for row in turns),
                   fallbackTurns=sum(row.get('usage',{}).get('streamStatus')=='safe_fallback' for row in turns if row.get('usage')),
                   fallbackRate=sum(row.get('usage',{}).get('streamStatus')=='safe_fallback' for row in turns if row.get('usage'))/len(turns) if turns else None,
                   retryTurnRate=sum(row['usage']['retryAttempts']>0 for row in turns if row.get('usage'))/len(turns) if turns else None,
                   completeUsageTurnRate=sum(row['usage']['tokensComplete'] for row in turns if row.get('usage'))/len(turns) if turns else None,
                   httpStatuses={str(code):sum(row['httpStatus']==code for row in turns) for code in {x['httpStatus'] for x in turns}},
                   clientEndToEnd=latency([row['clientEndToEndMs'] for row in turns]),
                   clientFirstVisibleToken=latency([row['clientFirstVisibleTokenMs'] for row in turns]),
                   serverFirstVisibleToken=latency([row['usage']['serverFirstTokenMs'] for row in turns if row.get('usage')]),
                   modelFirstToken=latency([row['firstTokenMs'] for row in attempts if row['operationStage']=='model.stream']),
                   observedWallSeconds=wall,
                   completedReplyThroughput=completed/wall if wall else None,
                   httpThroughput=len(turns)/wall if wall else None,
                   agentCostShares=cost_shares(attempts),
                   taskContract=verdict_rate([row['contractVerdict'] for row in items]),
                   mechanicalContract=verdict_rate(['pass' if all(all(turn['mechanicalChecks'].values()) for turn in row['turns']) else 'fail' for row in items]))
    by_stage = defaultdict(list)
    for row in attempts:
        by_stage[(row['agent'],row['stage'])].append(row)
    summary['byStage'] = [dict(agent=key[0],stage=key[1],**aggregate_usage(rows,[]),
                             duration=latency([r['durationMs'] for r in rows])) for key,rows in sorted(by_stage.items())]
    summary['judgedDimensions'] = {key:verdict_rate([row['judgeVerdicts'].get(key,'unknown') for row in items if key in row['requiredDimensions']])
                                   for key in ['goal','safety','grounding','clarification']}
    summary['labelledInjectedAttempts'] = sum(bool(row.get('injectedFault')) for row in attempts)
    summary['actualNetworkAttempts'] = len(attempts)-summary['labelledInjectedAttempts']
    errors = [event for row in turns for event in row.get('diagnostics',[])]
    summary['databaseDiagnostics'] = {str(code):sum(row.get('driverErrorCode')==code for row in errors) for code in {x.get('driverErrorCode') for x in errors}}
    return summary


def write_report(folder, manifest, observations, judgments, factory):
    from app.benchmark.runner import save
    from app.benchmark.protocol import load_dataset
    from app.services.model_gateway import aggregate_usage
    from app.models.entities import ModelUsageAttempt, ModelUsageRun
    from app.services.usage_reporting import attempts_csv
    dataset, _ = load_dataset()
    cases = {row['id']:row for row in dataset['nominal']+dataset['controlled']}
    mapped = {identity(row):row for row in judgments}
    for item in observations:
        case = cases.get(item['caseId'])
        judge = mapped.get(identity(item))
        if judge and judge['subjectDigest'] != subject_digest(case,item['turns']):
            raise ValueError('Judge subject changed')
        item['requiredDimensions'] = judge['requiredDimensions'] if judge else ['goal','safety']
        item['judgeVerdicts'] = {key:value['verdict'] for key,value in (judge.get('decision') or {}).items()} if judge else {}
        mechanical = all(all(turn['mechanicalChecks'].values()) for turn in item['turns'])
        item['contractVerdict'] = combine_contract(mechanical,[item['judgeVerdicts'].get(key,'unknown') for key in item['requiredDimensions']])
    groups, cohorts = {}, {}
    for variant in ['event_driven','sequential']:
        for concurrency in sorted({row['concurrency'] for row in observations if row['layer'] in {'nominal','context_intervention'}}):
            cohort = summarize([row for row in observations if row['variant']==variant and row['concurrency']==concurrency and row['layer'] in {'nominal','context_intervention'}])
            for field in ['taskContract','judgedDimensions','mechanicalContract']:
                cohort.pop(field)
            cohorts[f'{variant}/c{concurrency}'] = cohort
            for layer in ['nominal','context_intervention']:
                items = [row for row in observations if row['variant']==variant and row['concurrency']==concurrency and row['layer']==layer]
                if items: groups[f'{variant}/c{concurrency}/{layer}'] = summarize(items)
        for layer in ['injected_fault','admission_probe']:
            items = [row for row in observations if row['variant']==variant and row['layer']==layer]
            if items: groups[f'{variant}/{layer}'] = summarize(items)
    scope_attempts = defaultdict(list)
    attempt_locations = {}
    with factory() as db:
        for attempt, run in db.query(ModelUsageAttempt, ModelUsageRun).join(ModelUsageRun,ModelUsageRun.id==ModelUsageAttempt.run_id):
            scope_attempts[run.scope].append(json.loads(attempt.payload_json))
            attempt_locations[attempt.id] = {'runId':run.id,'scope':run.scope}
    costs = {scope: aggregate_usage(attempts,[]) for scope,attempts in scope_attempts.items()}
    costs['training_data_generation'] = {'status':'not_run','cost':None}
    admission = {}
    for variant in ['event_driven','sequential']:
        turns = [turn for row in observations if row['variant']==variant and row['layer']=='admission_probe' for turn in row['turns']]
        accepted = sum(turn['httpStatus']==200 for turn in turns)
        rejected = sum(turn.get('httpErrorCode')=='TURN_CAPACITY_EXCEEDED' for turn in turns)
        admission[variant] = {'offered':len(turns),'admitted':accepted,'capacityRejected':rejected,
                              'passed':len(turns)==5 and accepted==4 and rejected==1}
    paired = []
    for row in observations:
        if row['variant'] != 'event_driven' or row['layer'] not in {'nominal','context_intervention'}: continue
        other = next((x for x in observations if x['variant']=='sequential' and all(x[k]==row[k] for k in ['caseId','trial','concurrency','layer'])),None)
        if other:
            paired.append({'caseId':row['caseId'],'trial':row['trial'],'concurrency':row['concurrency'],
                           'eventDrivenVerdict':row['contractVerdict'],'sequentialVerdict':other['contractVerdict'],
                           'eventDrivenHttpMs':sum(t['clientEndToEndMs'] for t in row['turns']),
                           'sequentialHttpMs':sum(t['clientEndToEndMs'] for t in other['turns'])})
    evidence = {}
    for name in ['plan.json','private/observations.json','private/judgments.json']:
        path = folder/name
        if path.exists(): evidence[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Physical file and local SMTP receipt checks use only this benchmark's
    # unique subject prefix, never earlier business report IDs alone.
    from app.core.config import get_settings
    from app.models.entities import PsychologicalReport, ToolJob
    from openpyxl import load_workbook
    import httpx
    settings = get_settings()
    with factory() as db:
        report_ids = [row.id for row in db.query(PsychologicalReport)]
        high_ids = [row.id for row in db.query(PsychologicalReport).filter_by(risk_level='HIGH')]
        jobs = db.query(ToolJob).all()
        job_statuses = {status:sum(row.status==status for row in jobs) for status in {r.status for r in jobs}}
    from pathlib import Path
    excel_ids = []
    if Path(settings.excel_path).exists():
        workbook = load_workbook(settings.excel_path,read_only=True)
        try: excel_ids = [row[0] for row in workbook.active.iter_rows(min_row=2,values_only=True)]
        finally: workbook.close()
    messages = httpx.get('http://mailpit:8025/api/v1/messages?limit=200',timeout=10,trust_env=False).json()['messages']
    mail_marker = '[benchmark '+folder.name+']'
    import re
    received = [report_id for report_id in high_ids if any(mail_marker in m['Subject'] and re.search(r'reportId='+str(report_id)+r'(?!\d)',m['Subject']) for m in messages)]
    physical = {'reports':len(report_ids),'jobStatuses':job_statuses,
                'excelReportsWrittenOnce':sum(excel_ids.count(report_id)==1 for report_id in report_ids),
                'highRiskReports':len(high_ids),'mailpitReceipts':len(received),
                'smtpMeans':'Receipt by local Mailpit, not a human or real emergency service'}
    public = {'protocol':manifest['protocol'],'datasetSha256':manifest['datasetSha256'],
              'graderVersion':manifest.get('graderVersion','system-baseline-judge-v1'),
              'replyPolicyVersion':manifest.get('replyPolicyVersion','reply-safety-v3'),
              'sourceHashes':manifest['sourceHashes'],'plannedScenarios':len(manifest['plannedTrials']),
              'capturedScenarios':len(observations),'smoke':manifest['smoke'],'mock':manifest['mock'],
              'groups':groups,'cohortPerformance':cohorts,'costScopes':costs,'admission':admission,'pairedComparison':paired,
              'physicalEvidence':physical,'artifactHashes':evidence,
              'limitations':['Developer-visible synthetic labels, not expert gold or clinical validation.',
                             'Same-provider offline judge; invalid/missing judgments stay unknown.',
                             'Closed-loop client load; throughput includes harness pacing, not maximum server capacity.',
                             'Sequential variant runs later; shared provider cache/order can bias cost and latency.',
                             'Controlled empty retrieval/faults/admission probes are separate from nominal quality.',
                             'Cloud first_observed does not mean cold; no shared cache flush was performed.']}
    save(folder/'report.json', public)
    rows = []
    for item in observations:
        for turn in item['turns']:
            usage = turn.get('usage') or {}
            rows.append({'variant':item['variant'],'concurrency':item['concurrency'],'caseId':item['caseId'],'trial':item['trial'],
                         'sampleId':item['sampleId'],
                         'layer':item['layer'],'turnIndex':turn['turnIndex'],'label':turn['observationLabel'],
                         'requestId':turn['requestId'],'runId':usage.get('runId'),'httpStatus':turn['httpStatus'],
                         'clientMs':turn['clientEndToEndMs'],'clientFirstVisibleMs':turn['clientFirstVisibleTokenMs'],
                         'serverFirstVisibleMs':usage.get('serverFirstTokenMs'),'contract':item['contractVerdict'],
                         'totalTokens':(usage.get('tokens') or {}).get('totalTokens'),'usageCoverage':usage.get('usageCoverage'),
                         'costAmounts':json.dumps(usage.get('cost',{}).get('knownAmounts',{})),
                         'streamStatus':usage.get('streamStatus'),'retryAttempts':usage.get('retryAttempts')})
    output = io.StringIO(newline=''); writer = csv.DictWriter(output,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    (folder/'turns.csv').write_text(output.getvalue(),encoding='utf-8-sig')
    all_attempts = [attempt for values in scope_attempts.values() for attempt in values]
    base = list(csv.DictReader(io.StringIO(attempts_csv(all_attempts))))
    output = io.StringIO(newline='')
    if base:
        writer = csv.DictWriter(output,fieldnames=['runId','scope',*base[0]])
        writer.writeheader()
        writer.writerows({**row,**attempt_locations[row['id']]} for row in base)
    (folder/'attempts.csv').write_text(output.getvalue(),encoding='utf-8-sig')
    lines = ['# System baseline benchmark','',f'Dataset SHA256: `{manifest["datasetSha256"]}`',
             '',f'{len(observations)} / {len(manifest["plannedTrials"])} planned scenarios captured. Mock={manifest["mock"]}; smoke={manifest["smoke"]}.',
             '', '| Variant / concurrency / layer | Scenarios | Contract pass/fail/unknown | HTTP P50/P95 ms | Visible first P50/P95 ms | Done replies/s | Known token total |',
             '| --- | ---: | --- | --- | --- | ---: | ---: |']
    def number(value): return 'unknown' if value is None else f'{value:.2f}'
    for key, group in groups.items():
        score = group['taskContract']; e2e = group['clientEndToEnd']; first = group['clientFirstVisibleToken']
        lines.append(f'| {key} | {group["scenarios"]} | {score["pass"]}/{score["fail"]}/{score["unknown"]} | {number(e2e["p50Ms"])}/{number(e2e["p95Ms"])} | {number(first["p50Ms"])}/{number(first["p95Ms"])} | {number(group["completedReplyThroughput"])} | {(group["tokens"] or {}).get("totalTokens","unknown")} |')
    lines += ['', '## Interpretation limits', ''] + ['- '+text for text in public['limitations']]
    lines += ['', 'Costs, coverage, independent grounding/clarification verdicts, role shares and cache denominators: see report.json. Raw synthetic answers/judge evidence stay in private/.', '']
    (folder/'SUMMARY.md').write_text('\n'.join(lines),encoding='utf-8')
    return public
