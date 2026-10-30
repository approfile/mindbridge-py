"""Evaluate production Understanding/Safety routing without replies or side effects.

Each case has its own SQL session history. Only the two routing stages run;
RAG, reply generation/review, tool outbox and delivery are intentionally excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import tempfile
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.autonomous import UnderstandingAgent, SafetyAgent
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.agents.events import AgentTask
from app.agents.registry import AgentCapability
from app.core.config import get_settings
from app.core.database import Base
from app.evaluation.intent_dataset import DEFAULT_DATASET, ROOT, load_intent_dataset
from app.evaluation.intent_metrics import intent_metrics
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, ToolJob, UserAccount
from app.services.agent_models import AgentModelRegistry
from app.services.model_usage import measure_model_calls
from app.services.privacy import PrivacySanitizer


ROUTING_SOURCE_FILES = [
    'app/agents/autonomous.py', 'app/agents/event_driven_runtime.py', 'app/agents/coordinator.py',
    'app/services/assessment.py', 'app/services/risk_signals.py', 'app/services/ai.py',
    'app/services/agent_models.py', 'app/services/session_memory.py', 'app/services/memory.py',
    'app/services/privacy.py', 'app/core/config.py',
    'app/services/intent_contract.py',
    'app/services/observability.py', 'app/services/query_rewrite.py',
    'app/services/confidence.py', 'app/agents/plan_validation.py',
    'app/agents/events.py', 'app/agents/registry.py',
    'app/services/turn_budget.py',
    'app/core/privacy_crypto.py', 'app/models/entities.py', 'app/services/data_retention.py',
]


def source_hashes():
    return {file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in ROUTING_SOURCE_FILES}


def seed_case(db: Session, user, case, repeat: int):
    session = ChatSession(public_id=f"{case['id']}-repeat-{repeat}", user_id=user.id, title='Synthetic intent evaluation')
    db.add(session); db.flush()
    for item in case['history']:
        db.add(ChatMessage(user_id=user.id, session_id=session.id, role=item['role'].upper(), content=item['content']))
    if case['previousRisk']:
        level = case['previousRisk']
        db.add(PsychologicalReport(
            user_id=user.id, session_id=session.id, content='Synthetic prior-turn context, not a current prediction',
            intent='RISK' if level == 'HIGH' else 'CONSULT', emotion='HIGH_RISK' if level == 'HIGH' else 'ANXIETY',
            emotion_score=4 if level == 'HIGH' else 2, risk_level=level, confidence=1,
            summary='Fixture only: supplied prior risk, no outbox or delivery',
        ))
    db.commit()
    return session


def classify_turn(db: Session, settings, user, session, text: str) -> dict:
    """Run real agents and reuse the real runtime's final intent selection."""
    started = time.perf_counter(); calls = []
    runtime = EventDrivenAgentRuntimeService(db, settings)
    original_client_for = runtime.model_registry.client_for

    def observed_client_for(role):
        client = original_client_for(role)
        complete = client.complete
        def observed_complete(messages, **kwargs):
            call = {'role': role, 'succeeded': False}
            call_started = time.perf_counter()
            calls.append(call)
            try:
                raw = complete(messages, **kwargs)
                call.update(succeeded=True, output=raw,
                            outputSha256=hashlib.sha256(raw.encode()).hexdigest())
                return raw
            except Exception as exc:
                call['errorType'] = type(exc).__name__
                raise
            finally:
                call['elapsedMs'] = round((time.perf_counter() - call_started) * 1000, 3)
        client.complete = observed_complete
        return client
    runtime.model_registry.client_for = observed_client_for
    with measure_model_calls() as usage:
        services, board = runtime.prepare_turn(user, session, text, PrivacySanitizer().sanitize(text))
        for kind, agent_type, capability in [
            ('intent', UnderstandingAgent, AgentCapability.UNDERSTANDING),
            ('risk', SafetyAgent, AgentCapability.SAFETY),
        ]:
            task = AgentTask(id='intent-eval:' + kind, title=kind,
                             required_capabilities=frozenset({capability.value}), metadata={'kind': kind})
            agent = agent_type(services)
            board = board.add_task(task).apply_turn_result(task, agent.name, agent.act(task, board))
        result = runtime._to_result(board, user)
    proposal_payload = board.latest_artifact('intent').payload
    proposal = proposal_payload['intent']
    understanding_calls = [call for call in calls if call['role'] == 'UnderstandingAgent']
    valid_labels = {'CHAT', 'CONSULT', 'RISK'}
    understanding_source = proposal_payload.get('source') or ('rule' if not understanding_calls else (
        'model' if understanding_calls[-1].get('output', '').strip().upper().strip('`" \n') in valid_labels else 'fallback'
    ))
    assessment = result.assessment
    return {'proposedIntent': proposal, 'routedIntent': result.intent.value,
            'riskLevel': result.risk_level.value, 'understandingSource': understanding_source,
            'understandingReason': proposal_payload.get('reason'),
            'understandingErrorType': proposal_payload.get('errorType'),
            'assessmentSource': assessment.source, 'assessmentDegraded': assessment.degraded,
            'safetyChangedIntent': proposal != result.intent.value,
            'elapsedMs': round((time.perf_counter() - started) * 1000, 3),
            'modelCalls': calls, 'usage': usage}


def summarize_round(rows):
    categories = sorted({row['category'] for row in rows})
    times = sorted(row['elapsedMs'] for row in rows)
    return {'cases': len(rows), 'understanding': intent_metrics(rows, 'proposedIntent'),
            'finalRouting': intent_metrics(rows, 'routedIntent'),
            'categories': {group: intent_metrics([row for row in rows if row['category'] == group], 'routedIntent')
                           for group in categories},
            'understandingSources': dict(Counter(row['understandingSource'] for row in rows)),
            'assessmentSources': dict(Counter(row['assessmentSource'] for row in rows)),
            'degradedCases': sum(row['assessmentDegraded'] or row['understandingSource'] == 'fallback' for row in rows),
            'safetyChangedIntentCount': sum(row['safetyChangedIntent'] for row in rows),
            'apiAttempts': sum(row['usage']['apiAttempts'] for row in rows),
            'logicalCalls': sum(row['usage']['logicalCalls'] for row in rows),
            'failedModelRequests': sum(not call['succeeded'] for row in rows for call in row['modelCalls']),
            'meanRoutingMs': statistics.mean(times), 'p50RoutingMs': statistics.median(times),
            'p95RoutingMs': times[max(0, int(len(times) * .95 + .999999) - 1)],
            'failures': [{'id': row['id'], 'input': row['input'], 'expectedIntent': row['expectedIntent'],
                          'proposedIntent': row['proposedIntent'], 'routedIntent': row['routedIntent'],
                          'riskLevel': row['riskLevel'], 'rationale': row['rationale']}
                         for row in rows if row['routedIntent'] != row['expectedIntent']]}


def stability(rounds):
    def span(metric):
        values = [item['summary']['finalRouting'][metric] for item in rounds]
        return {'mean': statistics.mean(values), 'min': min(values), 'max': max(values)}
    by_id = {}
    for item in rounds:
        for row in item['results']:
            by_id.setdefault(row['id'], []).append(row['routedIntent'])
    unstable = {key: values for key, values in by_id.items() if len(set(values)) > 1}
    return {'uniqueCases': len(by_id), 'repeats': len(rounds), 'scoredAttempts': sum(len(r['results']) for r in rounds),
            'accuracy': span('accuracy'), 'macroF1': span('macroF1'), 'riskRecall': span('riskRecall'),
            'unstableCaseCount': len(unstable), 'unstablePredictions': unstable,
            'note': 'Primary result is repeat 1; range is across all runs, never the best run. Same cases repeated, not a larger independent test set.'}


def evaluate(settings, output: Path, *, repeats=1, seed=1729, dataset_path=DEFAULT_DATASET):
    if not 1 <= repeats <= 10:
        raise ValueError('repeats must be between 1 and 10')
    dataset = load_intent_dataset(dataset_path)
    initial_sources = source_hashes()
    registry = AgentModelRegistry(settings)
    profiles = {role: asdict(registry.profile_for(role)) for role in ['UnderstandingAgent', 'SafetyAgent']}
    report = {'createdAt': datetime.now(timezone.utc).isoformat(), 'complete': False,
              'dataset': {key: dataset[key] for key in ['version', 'datasetSha256', 'frozenVerified', 'annotation', 'labelPolicy', 'labelCounts']},
              'modelProfiles': profiles, 'openaiThinkingMode': settings.openai_thinking_mode,
              'deepseekAutoThinking': 'disabled', 'seed': seed, 'requestedRepeats': repeats,
              'sourceSha256': initial_sources, 'primaryMetric': 'rounds[0].summary.finalRouting',
              'tokenUsage': None, 'currencyCost': None,
              'scope': 'Production UnderstandingAgent + SafetyAgent and runtime final intent selection. '
                       'Fresh per-case SQL history/prior-risk fixtures, no replay of private agent memories. '
                       'No Coordinator scheduling/RAG/reply generation/review/tool execution. Redis disabled.',
              'limitations': 'Synthetic assistant-authored labels visible to developer; no independent annotation or held-out clinical population. '
                             'Rules can short-circuit model calls. RISK recall is routing recall, not diagnosis or clinical sensitivity. '
                             'Failures and fallback predictions stay in denominators. This developer-visible set may inform routing fixes; '
                             'a score after such fixes is regression evidence, not an unseen test score.',
              'rounds': []}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint():
        # Atomic checkpoint retains partial and failed results if interrupted.
        temporary = output.with_suffix(output.suffix + '.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temporary.replace(output)
    checkpoint()
    with tempfile.TemporaryDirectory(prefix='mindbridge-intent-eval-') as temporary:
        isolated = settings.model_copy(update={'database_url': f'sqlite:///{temporary}/intent.sqlite3',
                                             'knowledge_vector_enabled': False, 'knowledge_vector_required': False,
                                             'tool_queue_enabled': True, 'alert_email_delivery_mode': 'log'})
        engine = create_engine(isolated.database_url)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db, patch('app.services.memory.RedisShortTermMemoryStore._connect', return_value=None):
                user = UserAccount(username='intent-evaluation', display_name='Synthetic Evaluation', password_hash='unused')
                db.add(user); db.commit()
                for repeat in range(1, repeats + 1):
                    order = list(dataset['cases']); random.Random(seed + repeat - 1).shuffle(order)
                    record = {'repeat': repeat, 'caseOrder': [case['id'] for case in order], 'results': []}
                    report['rounds'].append(record)
                    for index, case in enumerate(order, 1):
                        started = time.perf_counter()
                        try:
                            session = seed_case(db, user, case, repeat)
                            prediction = classify_turn(db, isolated, user, session, case['input'])
                        except Exception as exc:
                            db.rollback()
                            prediction = {'proposedIntent': 'ERROR', 'routedIntent': 'ERROR', 'riskLevel': None,
                                          'understandingSource': 'error', 'assessmentSource': 'error', 'assessmentDegraded': True,
                                          'safetyChangedIntent': False, 'elapsedMs': (time.perf_counter() - started) * 1000,
                                          'modelCalls': [], 'usage': {'logicalCalls': 0, 'apiAttempts': 0}, 'errorType': type(exc).__name__}
                        row = {**case, **prediction}; record['results'].append(row)
                        checkpoint()
                        print(f"repeat={repeat} {index:03d}/100 {case['id']} gold={case['expectedIntent']} "
                              f"proposed={prediction['proposedIntent']} routed={prediction['routedIntent']} "
                              f"calls={prediction['usage']['apiAttempts']}", flush=True)
                    record['summary'] = summarize_round(record['results'])
                    checkpoint()
                report['toolJobsCreated'] = db.query(ToolJob).count()
                if report['toolJobsCreated']:
                    raise AssertionError('Intent evaluation unexpectedly created tool jobs')
        finally:
            engine.dispose()
    if source_hashes() != initial_sources or load_intent_dataset(dataset_path)['datasetSha256'] != dataset['datasetSha256']:
        raise ValueError('Routing code or dataset changed during evaluation')
    report['complete'] = True
    report['stability'] = stability(report['rounds'])
    report['completedAt'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return report


def main():
    from app.core.privacy_crypto import cipher
    cipher()  # explicit key initialization for this synthetic, isolated runner
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['mock', 'configured'], default='mock')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--output', default='target/intent-eval/report.json')
    args = parser.parse_args()
    settings = get_settings().model_copy(deep=True)
    if args.provider == 'mock':
        settings.ai_provider = settings.agent_model_default_provider = 'mock'
        settings.agent_model_default_model = 'mock'
        for role in ['understanding', 'safety']:
            setattr(settings, f'agent_model_{role}_provider', 'mock')
            setattr(settings, f'agent_model_{role}_model', 'mock')
    else:
        registry = AgentModelRegistry(settings)
        for role in ['UnderstandingAgent', 'SafetyAgent']:
            client = registry.client_for(role)
            if client.settings.ai_provider not in {'openai', 'ollama'}:
                parser.error('configured evaluation requires real model providers')
            if client.settings.ai_provider == 'openai' and not client.settings.openai_api_key:
                parser.error('Missing API credential for configured evaluation')
    report = evaluate(settings, Path(args.output), repeats=args.repeats, seed=args.seed)
    for item in report['rounds']:
        print(json.dumps({'repeat': item['repeat'], **item['summary']}, ensure_ascii=False, indent=2))
    print(json.dumps(report['stability'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
