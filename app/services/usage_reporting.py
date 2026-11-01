"""Read-only, bounded reports from durable attempt rows, never chat content."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import math

from app.models.entities import ModelUsageAttempt, ModelUsageRun, UsageCacheEvent
from app.services.model_gateway import aggregate_usage

MAX_ATTEMPTS = 20000
MAX_EVENTS = 20000


def _percentile(values, fraction):
    values = sorted(value for value in values if value is not None)
    return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 3) if values else None


def _latency(rows, key):
    values = [row.get(key) for row in rows if row.get(key) is not None]
    return {'samples': len(values), 'p50Ms': _percentile(values, .5), 'p95Ms': _percentile(values, .95)}


def usage_report(db, hours=24, scope='online_chat', now=None):
    now = now or datetime.now(timezone.utc)
    since = now.replace(tzinfo=None) - timedelta(hours=hours)
    # Filter by attempts/events' own timestamps; a process failure can leave an
    # unfinished run. Do not trust a run snapshot's possibly stale totals.
    attempt_query = db.query(ModelUsageAttempt).outerjoin(ModelUsageRun, ModelUsageRun.id == ModelUsageAttempt.run_id).filter(ModelUsageAttempt.created_at >= since)
    cache_query = db.query(UsageCacheEvent).outerjoin(ModelUsageRun, ModelUsageRun.id == UsageCacheEvent.run_id).filter(UsageCacheEvent.created_at >= since)
    runs_query = db.query(ModelUsageRun).filter(ModelUsageRun.created_at >= since)
    if scope != 'all':
        attempt_query = attempt_query.filter(ModelUsageRun.scope == scope)
        cache_query = cache_query.filter(ModelUsageRun.scope == scope)
        runs_query = runs_query.filter(ModelUsageRun.scope == scope)
    attempts = attempt_query.order_by(ModelUsageAttempt.created_at.desc(), ModelUsageAttempt.id).limit(MAX_ATTEMPTS + 1).all()
    events = cache_query.order_by(UsageCacheEvent.created_at.desc(), UsageCacheEvent.id).limit(MAX_EVENTS + 1).all()
    truncated = len(attempts) > MAX_ATTEMPTS or len(events) > MAX_EVENTS
    rows = [json.loads(row.payload_json) for row in attempts[:MAX_ATTEMPTS]]
    cache_rows = [json.loads(row.payload_json) for row in events[:MAX_EVENTS]]
    result = aggregate_usage(rows, cache_rows)
    result.update(windowHours=hours, scope=scope, generatedAt=now.isoformat(), truncated=truncated,
                  rowLimits={'attempts': MAX_ATTEMPTS, 'cacheEvents': MAX_EVENTS})
    groups = defaultdict(list)
    for row in rows:
        groups[(row['agent'], row['stage'], row['operation'])].append(row)
    result['byStage'] = [dict(agent=key[0], stage=key[1], operation=key[2],
                              **aggregate_usage(items, []), latency=_latency(items, 'durationMs'),
                              firstTokenLatency=_latency(items, 'firstTokenMs'))
                         for key, items in sorted(groups.items())]
    # SQL count is exact; latency reads are bounded and explicitly labelled.
    result['runCount'] = runs_query.count()
    finalized = runs_query.filter(ModelUsageRun.finalized.is_(True)).order_by(ModelUsageRun.created_at.desc()).limit(2000).all()
    snapshots = [json.loads(row.payload_json) for row in finalized]
    result['endToEndLatency'] = _latency(snapshots, 'durationMs')
    result['serverFirstTokenLatency'] = _latency(snapshots, 'serverFirstTokenMs')
    result['latencySampleLimit'] = 2000
    result['unfinishedRuns'] = runs_query.filter(ModelUsageRun.finalized.is_(False)).count()
    result['ledgerWriteErrorsInFinalizedRuns'] = sum(row.get('ledgerWriteErrors', 0) for row in snapshots)
    result['coverageNotice'] = 'Coverage describes recorded attempts only. Ledger write failures are also logged; failed writes cannot be reconstructed from this ledger.'
    return result


def list_usage_runs(db, limit=20, scope='online_chat'):
    query = db.query(ModelUsageRun)
    if scope != 'all':
        query = query.filter(ModelUsageRun.scope == scope)
    runs = query.order_by(ModelUsageRun.created_at.desc(), ModelUsageRun.id).limit(limit).all()
    ids = [row.id for row in runs]
    attempts, events = defaultdict(list), defaultdict(list)
    if ids:
        for row in db.query(ModelUsageAttempt).filter(ModelUsageAttempt.run_id.in_(ids)):
            attempts[row.run_id].append(json.loads(row.payload_json))
        for row in db.query(UsageCacheEvent).filter(UsageCacheEvent.run_id.in_(ids)):
            events[row.run_id].append(json.loads(row.payload_json))
    result = []
    for row in runs:
        snapshot = json.loads(row.payload_json)
        result.append({**snapshot, **aggregate_usage(attempts[row.id], events[row.id]),
                       'runId': row.id, 'userId': row.user_id, 'scope': row.scope, 'finalized': row.finalized,
                       'createdAt': row.created_at.replace(tzinfo=timezone.utc).isoformat()})
    return result


def usage_run_detail(db, run_id):
    row = db.get(ModelUsageRun, run_id)
    if row is None:
        return None
    attempts = [json.loads(item.payload_json) for item in db.query(ModelUsageAttempt).filter_by(run_id=run_id).order_by(ModelUsageAttempt.created_at, ModelUsageAttempt.id)]
    events = [json.loads(item.payload_json) for item in db.query(UsageCacheEvent).filter_by(run_id=run_id).order_by(UsageCacheEvent.created_at, UsageCacheEvent.id)]
    return {**json.loads(row.payload_json), **aggregate_usage(attempts, events),
            'runId': run_id, 'userId': row.user_id, 'scope': row.scope, 'finalized': row.finalized,
            'attempts': attempts, 'cacheEvents': events}


def attempts_csv(attempts):
    fields = ['id', 'logicalCallId', 'agent', 'stage', 'operation', 'provider', 'host',
              'requestedModel', 'actualModel', 'providerRequestId', 'startedAt', 'attemptNumber',
              'status', 'httpStatus', 'errorType', 'durationMs', 'firstTokenMs', 'usageStatus',
              'inputTokens', 'outputTokens', 'totalTokens', 'cacheHitTokens', 'cacheMissTokens', 'reasoningTokens',
              'costStatus', 'currency', 'amount', 'priceVersion', 'priceSource']
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in attempts:
        cost = row.get('cost', {})
        values = {**row, 'costStatus': cost.get('status'), **{key: cost.get(key) for key in ['currency', 'amount', 'priceVersion', 'priceSource']}}
        # Keep exported string metadata inert in spreadsheet applications.
        values = {key: "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for key, value in values.items()}
        writer.writerow(values)
    return output.getvalue()
