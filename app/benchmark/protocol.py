"""Frozen inputs and deterministic statistics. No network calls in this module."""
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

DATASET = Path(__file__).with_name('dataset-v1.json')


def load_dataset():
    raw = DATASET.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DATASET.with_suffix('.sha256').read_text().strip():
        raise ValueError('Frozen benchmark dataset hash mismatch')
    data = json.loads(raw)
    cases = data['nominal'] + data['controlled']
    if len({item['id'] for item in cases}) != len(cases):
        raise ValueError('Duplicate case ID')
    return data, digest


def percentile(values, quantile):
    values = sorted(x for x in values if x is not None and math.isfinite(x))
    return round(values[max(0, math.ceil(len(values)*quantile)-1)], 3) if values else None


def latency(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    return {'samples': len(values), 'p50Ms': percentile(values, .5), 'p95Ms': percentile(values, .95)}


def verdict_rate(verdicts):
    counts = Counter(verdicts)
    total = len(verdicts)
    known = counts['pass'] + counts['fail']
    return {'samples': total, 'pass': counts['pass'], 'fail': counts['fail'], 'unknown': total-known,
            'rate': counts['pass']/total if total and known == total else None,
            'lowerBound': counts['pass']/total if total else None,
            'upperBound': (total-counts['fail'])/total if total else None}


def combine_contract(mechanical, judgments):
    verdicts = list(judgments)
    if not mechanical or 'fail' in verdicts:
        return 'fail'
    if not verdicts or any(v != 'pass' for v in verdicts):
        return 'unknown'
    return 'pass'


def cost_shares(attempts):
    totals, agents = {}, {}
    for row in attempts:
        cost = row.get('cost', {})
        if cost.get('status') != 'priced':
            continue
        currency = cost['currency']; amount = Decimal(cost['amount'])
        totals[currency] = totals.get(currency, Decimal(0)) + amount
        agents.setdefault(row['agent'], {})
        agents[row['agent']][currency] = agents[row['agent']].get(currency, Decimal(0)) + amount
    return {agent: {currency: {'knownAmount': str(amount), 'shareOfKnownCost': float(amount/totals[currency]) if totals[currency] else None}
                    for currency, amount in currencies.items()} for agent, currencies in agents.items()}


def subject_digest(case, turns):
    evidence = {'case': case, 'turns': [{key: row.get(key) for key in ['requestId', 'visibleText', 'retrievedContexts', 'mechanicalChecks']} for row in turns]}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_judgment(value, visible, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError('Judge dimensions mismatch')
    for row in value.values():
        if not isinstance(row, dict) or set(row) != {'verdict', 'reason', 'quotes'}:
            raise ValueError('Judge schema mismatch')
        if row['verdict'] not in {'pass', 'fail', 'unknown'} or not isinstance(row['reason'], str) or not 0 < len(row['reason']) <= 1200:
            raise ValueError('Invalid judge verdict')
        if not isinstance(row['quotes'], list) or any(not isinstance(q, str) or not q or q not in visible for q in row['quotes']):
            raise ValueError('Judge quote is not from visible answer')
        if visible and row['verdict'] != 'unknown' and not row['quotes']:
            raise ValueError('Missing judge evidence')
    return value
