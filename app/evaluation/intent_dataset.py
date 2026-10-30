"""Strict frozen intent contracts, deliberately separate from RAG/risk scores."""
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABELS = ('CHAT', 'CONSULT', 'RISK')
DEFAULT_DATASET = ROOT / 'app/evaluation/data/intent-v1.json'


def load_intent_dataset(path: str | Path = DEFAULT_DATASET) -> dict:
    file = Path(path)
    if not file.is_absolute():
        file = ROOT / file
    raw = file.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    manifest = json.loads((ROOT / 'app/evaluation/data/frozen-manifest.json').read_text())
    try:
        relative = file.resolve().relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ValueError('Intent evaluation requires a registered frozen dataset') from exc
    if manifest.get(relative) != digest:
        raise ValueError('Intent dataset is not frozen or its content changed; create a new version')
    data = json.loads(raw)
    if data.get('schemaVersion') != 1 or set(data.get('labelPolicy', {})) != {*LABELS, 'precedence'}:
        raise ValueError('Invalid intent dataset schema or label policy')
    cases = data['cases']
    if len(cases) != 100:
        raise ValueError('intent-v1 requires exactly 100 unique cases')
    ids, texts = set(), set()
    for case in cases:
        if not all(isinstance(case.get(key), str) and case[key].strip()
                   for key in ['id', 'input', 'category', 'rationale', 'language']):
            raise ValueError('Missing case identity, input, category or label rationale')
        text = ''.join(case['input'].lower().split())
        if case['id'] in ids or text in texts or case['expectedIntent'] not in LABELS:
            raise ValueError('Duplicate case or invalid intent label')
        ids.add(case['id']); texts.add(text)
        if not isinstance(case['history'], list) or any(
            item.get('role') not in {'user', 'assistant'} or not isinstance(item.get('content'), str)
            or not item['content'].strip() for item in case['history']
        ):
            raise ValueError('Invalid history: only user/assistant data is allowed')
        if case['previousRisk'] not in {None, 'LOW', 'MEDIUM', 'HIGH'}:
            raise ValueError('Invalid previous risk')
        if case['previousRisk'] and not any(item['role'] == 'user' for item in case['history']):
            raise ValueError('Previous risk requires explicit historical context')
    counts = dict(Counter(case['expectedIntent'] for case in cases))
    if counts != {label: data['sampling'][label] for label in LABELS}:
        raise ValueError('Label distribution differs from the frozen sampling contract')
    return {**data, 'datasetSha256': digest, 'frozenVerified': True, 'labelCounts': counts}
