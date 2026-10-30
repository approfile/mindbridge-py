"""Frozen engineering evaluations; isolated data, no tool workers or email.

These synthetic contracts measure implementation behavior, not clinical accuracy.
The sequential comparator shares capabilities; only scheduling differs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import Base
from app.evaluation.sequential import SequentialWorkflowBaseline
from app.models.entities import AgentRunTrace, ChatSession, ToolJob, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.agent_models import AgentModelRegistry
from app.services.assessment import PsychologicalAssessmentService
from app.services.chat import ChatService
from app.services.knowledge import KnowledgeService
from app.services.model_usage import measure_model_calls


ROOT = Path(__file__).resolve().parents[2]


def load_frozen(name: str) -> dict:
    relative = f'app/evaluation/data/{name}-v1.json'
    file = ROOT / relative
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    manifest = json.loads((ROOT / 'app/evaluation/data/frozen-manifest.json').read_text())
    if manifest.get(relative) != digest:
        raise ValueError('Frozen evaluation changed; create a new version')
    return {**json.loads(file.read_text()), 'datasetSha256': digest, 'frozenVerified': True}


def classification_metrics(rows: list[dict]) -> dict:
    labels = ['LOW', 'MEDIUM', 'HIGH']
    confusion = {gold: {pred: 0 for pred in labels} for gold in labels}
    for row in rows:
        confusion[row['expected']][row['actual']] += 1
    per_class = {}
    for label in labels:
        tp = confusion[label][label]
        support = sum(confusion[label].values())
        predicted = sum(confusion[gold][label] for gold in labels)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        per_class[label] = dict(precision=precision, recall=recall, support=support,
                               f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return {'cases': len(rows), 'accuracy': sum(row['expected'] == row['actual'] for row in rows) / len(rows),
            'macroF1': sum(item['f1'] for item in per_class.values()) / len(labels),
            'confusionMatrix': confusion, 'perClass': per_class}


def evaluate_risk(settings: Settings, limit: int | None = None) -> dict:
    dataset = load_frozen('risk')
    ai = AgentModelRegistry(settings).client_for('SafetyAgent')
    rows = []
    for case in dataset['cases'][:limit]:
        started = time.perf_counter()
        with measure_model_calls() as usage:
            result = PsychologicalAssessmentService(ai).assess(
                case['input'], [AiMessage(role='user', content=text) for text in case['history']], case['previousRisk'],
            )
        rows.append({'id': case['id'], 'expected': case['expectedRisk'], 'actual': result.risk.value,
                     'source': result.source, 'degraded': result.degraded, 'reasonCodes': result.reason_codes,
                     'elapsedMs': round((time.perf_counter() - started) * 1000, 3), 'usage': usage})
        print(f"risk {case['id']}: {result.risk.value}", flush=True)
    return {**dataset_contract(dataset), **classification_metrics(rows), 'results': rows,
            'degradedRate': sum(row['degraded'] for row in rows) / len(rows),
            'apiAttempts': sum(row['usage']['apiAttempts'] for row in rows)}


def dataset_contract(dataset):
    return {key: dataset[key] for key in ['version', 'datasetSha256', 'frozenVerified', 'annotation']}


def answer_checks(text: str, case: dict) -> dict:
    checks = {}
    for term in case.get('answerContains', []):
        checks['contains:' + term] = term in text
    for term in case.get('answerExcludes', []):
        checks['excludes:' + term] = term not in text
    if case.get('answerContainsAny'):
        checks['containsAny'] = any(term in text for term in case['answerContainsAny'])
    return checks


def parse_sse(block: str):
    event = next(line[7:] for line in block.splitlines() if line.startswith('event: '))
    data = json.loads(next(line[6:] for line in block.splitlines() if line.startswith('data: ')))
    return event, data


async def evaluate_dialogues(settings: Settings, architecture: str, limit: int | None = None) -> dict:
    dataset = load_frozen('dialogues')
    results = []
    with tempfile.TemporaryDirectory(prefix='mindbridge-dialogue-eval-') as temporary:
        isolated = settings.model_copy(update={
            'database_url': f'sqlite:///{temporary}/evaluation.sqlite3',
            'knowledge_vector_enabled': False, 'knowledge_vector_required': False,
            'alert_email_delivery_mode': 'log', 'tool_queue_enabled': True,
        })
        engine = create_engine(isolated.database_url)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db, patch('app.services.memory.RedisShortTermMemoryStore._connect', return_value=None):
                knowledge = KnowledgeService(db, isolated)
                for file in sorted((ROOT / 'app/knowledge').glob('*.md')):
                    knowledge.ensure_source(file.name, file.read_text())
                factory = SequentialWorkflowBaseline if architecture == 'sequential' else None
                service = ChatService(db, isolated, runtime_factory=factory)
                for case in dataset['cases'][:limit]:
                    user = UserAccount(username=case['id'], display_name='Synthetic Evaluation', password_hash='unused')
                    db.add(user)
                    db.flush()
                    session = ChatSession(public_id=case['id'], user_id=user.id, title='Synthetic Evaluation')
                    db.add(session)
                    db.commit()
                    turns = []
                    for message in case['turns']:
                        started = time.perf_counter()
                        first_token_ms = None
                        events = []
                        with measure_model_calls() as usage:
                            async for block in service.stream_chat(user, ChatRequest(message=message, sessionId=session.public_id)):
                                event, data = parse_sse(block)
                                if event == 'token' and first_token_ms is None:
                                    first_token_ms = (time.perf_counter() - started) * 1000
                                events.append((event, data))
                        text = ''.join(data.get('content') or '' for event, data in events if event == 'token')
                        trace = db.query(AgentRunTrace).filter_by(session_id=session.id).order_by(AgentRunTrace.id.desc()).first()
                        steps = json.loads(trace.agent_steps_json) if trace else []
                        turns.append({'input': message, 'answer': text, 'intent': trace.intent if trace else None,
                                      'risk': trace.risk_level if trace else None, 'accepted': any(e == 'done' for e, _ in events),
                                      'bodyReview': [s for s in steps if s.get('kind') in {'response_review', 'response_outcome'}],
                                      'eventCount': sum(s.get('kind') == 'agent_event' for s in steps),
                                      'elapsedMs': round((time.perf_counter() - started) * 1000, 3),
                                      'firstTokenMs': first_token_ms, 'usage': usage})
                    final = turns[-1]
                    checks = answer_checks(final['answer'], case)
                    checks['allTurnsAccepted'] = all(turn['accepted'] for turn in turns)
                    for expected, actual in [('expectedRisk', 'risk'), ('expectedIntent', 'intent')]:
                        if expected in case:
                            checks[expected] = case[expected] == final[actual]
                    results.append({'id': case['id'], 'checks': checks, 'passed': all(checks.values()), 'turns': turns})
                    print(f"{architecture} {case['id']}: {'PASS' if all(checks.values()) else 'FAIL'}", flush=True)
                planned = db.query(ToolJob).count()
        finally:
            engine.dispose()
    turns = [turn for row in results for turn in row['turns']]
    latencies = sorted(turn['elapsedMs'] for turn in turns)
    return {**dataset_contract(dataset), 'architecture': architecture, 'cases': len(results),
            'passed': sum(row['passed'] for row in results), 'turns': len(turns),
            'acceptanceRate': sum(turn['accepted'] for turn in turns) / len(turns),
            'meanTurnMs': sum(latencies) / len(latencies), 'p95TurnMs': latencies[max(0, int(len(latencies) * .95 + .999999) - 1)],
            'apiAttempts': sum(turn['usage']['apiAttempts'] for turn in turns),
            'logicalCalls': sum(turn['usage']['logicalCalls'] for turn in turns),
            'plannedToolJobs': planned, 'executedToolJobs': 0,
            'retrievalMode': 'bm25-rerank', 'memoryBackend': 'database fallback', 'results': results}


def main():
    from app.core.privacy_crypto import cipher
    cipher()  # explicit initialization; all evaluation SQL data is synthetic
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['risk', 'dialogues', 'all'], default='all')
    parser.add_argument('--provider', choices=['mock', 'configured'], default='mock')
    parser.add_argument('--architecture', choices=['event', 'sequential', 'both'], default='both')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--output', default='target/p1-evaluation.json')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    settings = get_settings().model_copy(deep=True)
    if args.provider == 'mock':
        settings.ai_provider = 'mock'
        settings.agent_model_default_provider = 'mock'
        settings.agent_model_default_model = 'mock'
        for role in ['understanding', 'safety', 'context', 'response', 'coordinator']:
            setattr(settings, f'agent_model_{role}_provider', 'mock')
            setattr(settings, f'agent_model_{role}_model', 'mock')
    elif any(AgentModelRegistry(settings).profile_for(role).provider not in {'openai', 'ollama'}
             for role in ['UnderstandingAgent', 'SafetyAgent', 'ResponseAgent']):
        parser.error('configured evaluation requires real supported model providers')
    profiles = {role: asdict(AgentModelRegistry(settings).profile_for(role))
                for role in ['UnderstandingAgent', 'SafetyAgent', 'ResponseAgent']}
    report = {'createdAt': datetime.now(timezone.utc).isoformat(), 'providerMode': args.provider,
              'modelProfiles': profiles, 'limit': args.limit, 'agentMaxRounds': settings.agent_max_rounds,
              'replyMaxRevisions': settings.reply_max_revisions, 'replyTimeoutSeconds': settings.reply_timeout_seconds,
              'openaiThinkingMode': settings.openai_thinking_mode,
              'thinkingModeNote': 'auto disables reasoning for api.deepseek.com; other endpoints keep provider defaults',
              'replyReviewJsonMode': settings.reply_review_json_mode,
              'isolatedDatabase': True, 'repeats': 1, 'tokenUsage': None, 'currencyCost': None,
              'limitations': 'Synthetic engineering checks; no clinical validation or blind human judging. '
                             'Fixed-order comparator shares agents/policies/reply gate; not a single-call LLM. '
                             'Tool jobs are planned only. One run; model variance and latency are not statistically estimated.'}
    if args.suite in {'risk', 'all'}:
        report['risk'] = evaluate_risk(settings, args.limit)
    if args.suite in {'dialogues', 'all'}:
        architectures = ['event', 'sequential'] if args.architecture == 'both' else [args.architecture]
        report['dialogues'] = {name: asyncio.run(evaluate_dialogues(settings, name, args.limit)) for name in architectures}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: {k: v for k, v in value.items() if k != 'results'} if key == 'risk' else value
                      for key, value in report.items() if key != 'dialogues'}, ensure_ascii=False, indent=2))
    if 'dialogues' in report:
        print(json.dumps({name: {key: value for key, value in result.items() if key != 'results'}
                          for name, result in report['dialogues'].items()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
