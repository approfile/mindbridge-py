"""Frozen contrast cases for reference attribution, support intent and severity."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base
from app.evaluation.intent_metrics import intent_metrics
from app.evaluation.intent_runner import classify_turn, seed_case, source_hashes
from app.models.entities import ToolJob, UserAccount
from app.services.agent_models import AgentModelRegistry

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / 'app/evaluation/data/context-routing-v1.json'


def evaluate(settings, output: Path, *, repeats=1):
    raw = DATASET.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    manifest = json.loads((ROOT / 'app/evaluation/data/frozen-manifest.json').read_text())
    if manifest.get(str(DATASET.relative_to(ROOT))) != digest:
        raise ValueError('Boundary dataset differs from frozen manifest')
    dataset = json.loads(raw)
    cases = dataset['cases']
    if len({c['id'] for c in cases}) != len(cases) or any(
        c['expectedIntent'] not in {'CHAT', 'CONSULT', 'RISK'}
        or c['expectedRisk'] not in {'LOW', 'MEDIUM', 'HIGH'} for c in cases
    ):
        raise ValueError('Invalid boundary labels or duplicate identities')
    if not 1 <= repeats <= 10:
        raise ValueError('repeats must be between 1 and 10')
    sources = source_hashes()
    report = {'complete': False, 'datasetSha256': digest, 'annotation': dataset['annotation'],
              'createdAt': datetime.now(timezone.utc).isoformat(), 'seed': 1729,
              'modelProfiles': {role: asdict(AgentModelRegistry(settings).profile_for(role))
                                for role in ['UnderstandingAgent', 'SafetyAgent']},
              'sourceSha256': sources, 'rounds': [], 'scope': 'Same production routing stages as intent_runner; no replies/tools.'}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint():
        tmp = output.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2)); tmp.replace(output)
    with tempfile.TemporaryDirectory() as temporary:
        isolated = settings.model_copy(update={'database_url': f'sqlite:///{temporary}/context.sqlite3',
                                               'knowledge_vector_enabled': False, 'knowledge_vector_required': False})
        engine = create_engine(isolated.database_url)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db, patch('app.services.memory.RedisShortTermMemoryStore._connect', return_value=None):
                user = UserAccount(username='context-eval', display_name='Synthetic', password_hash='unused')
                db.add(user); db.commit()
                for repeat in range(1, repeats + 1):
                    cases = list(dataset['cases']); random.Random(1729 + repeat - 1).shuffle(cases)
                    record = {'repeat': repeat, 'results': []}; report['rounds'].append(record)
                    for case in cases:
                        try:
                            session = seed_case(db, user, case, repeat)
                            prediction = classify_turn(db, isolated, user, session, case['input'])
                        except Exception as exc:
                            db.rollback()
                            prediction = {'proposedIntent': 'ERROR', 'routedIntent': 'ERROR', 'riskLevel': 'ERROR',
                                          'errorType': type(exc).__name__}
                        record['results'].append({**case, **prediction})
                        checkpoint()
                        print(repeat, case['id'], prediction['routedIntent'], prediction['riskLevel'], flush=True)
                    rows = record['results']
                    record['intentMetrics'] = intent_metrics(rows, 'routedIntent')
                    record['jointCorrect'] = sum(r['expectedIntent'] == r['routedIntent'] and r['expectedRisk'] == r['riskLevel'] for r in rows)
                    record['failures'] = [r['id'] for r in rows if r['expectedIntent'] != r['routedIntent'] or r['expectedRisk'] != r['riskLevel']]
                    checkpoint()
                report['toolJobsCreated'] = db.query(ToolJob).count()
                assert report['toolJobsCreated'] == 0
        finally:
            engine.dispose()
    if sources != source_hashes() or hashlib.sha256(DATASET.read_bytes()).hexdigest() != digest:
        raise ValueError('Routing code or dataset changed during evaluation')
    report['complete'] = True; checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['mock', 'configured'], default='mock')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--output', default='target/intent-eval/context-routing.json')
    args = parser.parse_args()
    settings = get_settings().model_copy(deep=True)
    if args.provider == 'mock':
        settings.ai_provider = settings.agent_model_default_provider = 'mock'
        for role in ['understanding', 'safety']:
            setattr(settings, f'agent_model_{role}_provider', 'mock')
    else:
        for role in ['UnderstandingAgent', 'SafetyAgent']:
            client = AgentModelRegistry(settings).client_for(role)
            if client.settings.ai_provider not in {'openai', 'ollama'}:
                parser.error('configured requires a real model provider')
    report = evaluate(settings, Path(args.output), repeats=args.repeats)
    print(json.dumps([{'repeat': r['repeat'], 'jointCorrect': r['jointCorrect'], 'failures': r['failures']}
                      for r in report['rounds']], indent=2))


if __name__ == '__main__':
    main()
