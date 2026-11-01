"""Paid, small synthetic smoke test; run inside the existing local app container.

Makes three direct generation/embedding probes and three online turns. Exports
metadata only. Does not delete shared caches or export conversation bodies.
"""
from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.model_gateway import ModelAttempt, aggregate_usage, bind_ledger, persist_run, usage_snapshot
from app.services.observability import observation_scope
from app.services.usage_reporting import attempts_csv
from app.services.vector_store import ChromaKnowledgeStore


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    settings = get_settings()
    require(settings.ai_provider == 'openai' and settings.knowledge_vector_enabled, 'Start the local stack with -RealModels first')
    output = Path('target/model-usage-verification') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'rawUsageComparisons': [], 'onlineRuns': [], 'output': str(output)}
    raw_by_attempt = {}
    original_capture = ModelAttempt.capture

    def capture(attempt, data):
        # A separate audit copy retains ONLY usage and model identifiers.
        if isinstance(data, dict) and isinstance(data.get('usage'), dict):
            raw_by_attempt[attempt.row['id']] = {'usage': data['usage'], 'model': data.get('model'), 'id': data.get('id') or data.get('request_id')}
        return original_capture(attempt, data)

    try:
        with observation_scope(ensure_run=True, reuse=False) as observation:
            bind_ledger(observation, SessionLocal, scope='validation_probe')
            try:
                with patch.object(ModelAttempt, 'capture', capture):
                    client = AiClient(settings); client.agent_name = 'VerificationProbe'
                    messages = [AiMessage(role='user', content='工程验收的虚构请求，请只回答：测试通过。')]
                    client.complete(messages)
                    async def stream():
                        return ''.join([token async for token in client.stream(messages)])
                    asyncio.run(stream())
                    # Independent synthetic text; does not read the user's KB.
                    store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
                    store.settings = settings; store.can_embed = True
                    store.embed_texts(['虚构工程验收：校园学习时间安排。'])
            finally:
                persist_run(observation, finalized=True)
            snapshot = usage_snapshot(observation)
        require(len(snapshot['attempts']) == 3, 'Direct probe attempt count')
        for row in snapshot['attempts']:
            raw = raw_by_attempt.get(row['id'])
            require(raw is not None, 'Provider did not return auditable usage')
            usage = raw['usage']
            expected_input = usage['prompt_tokens']
            expected_output = usage.get('completion_tokens', 0) if row['operation'] == 'embedding' else usage['completion_tokens']
            require(row['inputTokens'] == expected_input and row['outputTokens'] == expected_output, 'Raw usage differs from ledger')
            require(row['totalTokens'] == usage['total_tokens'] == expected_input + expected_output, 'Raw total differs from ledger')
            if 'prompt_cache_hit_tokens' in usage:
                require(row['cacheHitTokens'] == usage['prompt_cache_hit_tokens'], 'Raw cache hit differs from ledger')
                require(row['cacheMissTokens'] == usage['prompt_cache_miss_tokens'], 'Raw cache miss differs from ledger')
            require(row['usageStatus'] == 'reported' and row['status'] == 'completed', 'Unfinished direct probe')
            report['rawUsageComparisons'].append({'attemptId': row['id'], 'rawProviderMetadata': raw, 'ledger': row, 'matched': True})
        require(not snapshot['ledgerWriteErrors'], 'Direct probe ledger writes failed')
        report['probeRunId'] = observation.ids['run_id']
        print('PASS direct non-stream, stream and embedding usage equals raw provider fields', flush=True)

        with httpx.Client(base_url='http://127.0.0.1:8080', timeout=90, trust_env=False) as http:
            def get(path):
                result = http.get(path, auth=('admin', 'admin123')); result.raise_for_status(); return result.json()
            session_id = None
            cases = [('chat', '你好，这是工程验收的虚构对话。请用一句话介绍如何安排今天的学习。'),
                     ('follow_up', '请再给我一个简短的学习安排建议。'),
                     ('consult', '这是工程验收的虚构情境：我因考试有点紧张，可以陪我聊两句吗？')]
            for name, message in cases:
                payload = {'message': message}
                if name == 'follow_up': payload['sessionId'] = session_id
                result = http.post('/api/chat/stream', auth=('student', 'student123'), json=payload)
                result.raise_for_status()
                request_id = result.headers.get('x-request-id')
                require(request_id, 'Missing correlation header')
                events = []
                for block in result.text.strip().split('\n\n'):
                    data = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line)
                    if 'event' in data and 'data' in data: events.append((data['event'], json.loads(data['data'])))
                require(any(kind == 'token' for kind, _ in events), 'No visible response')
                if name == 'chat': session_id = next(data['sessionId'] for kind, data in events if kind == 'meta')
                deadline = time.monotonic() + 10
                while True:
                    run = next((row for row in get('/api/admin/model-usage?limit=100') if row.get('correlation', {}).get('request_id') == request_id), None)
                    if run and run['finalized']: break
                    require(time.monotonic() < deadline, 'Missing finalized usage run')
                    time.sleep(.25)
                detail = get('/api/admin/model-usage/'+run['runId'])
                require(detail['httpAttempts'] > 0 and detail['unknownUsageAttempts'] == 0, 'Online usage coverage incomplete')
                require(detail['ledgerWriteErrors'] == 0 and detail['userId'] is not None, 'Ledger/user attribution missing')
                require(all(row['agent'] != 'Unassigned' for row in detail['attempts']), 'Missing agent attribution')
                detail['verificationCase'] = name
                detail['sseOutcome'] = 'done' if any(kind == 'done' for kind, _ in events) else 'fallback_or_error'
                report['onlineRuns'].append(detail)
                print(f"PASS {name}: {detail['httpAttempts']} attempts; {detail['tokens']['totalTokens']} tokens; {detail['sseOutcome']}", flush=True)
            layers = aggregate_usage([], [event for run in report['onlineRuns'] for event in run['cacheEvents']])['cacheLayers']
            require(layers.get('redis_session_history', {}).get('hits', 0) >= 1, 'Warm session history hit not observed')
            require(any(row['operation'] == 'embedding' for run in report['onlineRuns'] for row in run['attempts']), 'Online query embedding not observed')
            require(any(run['sseOutcome'] == 'done' for run in report['onlineRuns']), 'All synthetic replies fell back')
            report['onlineSummary'] = aggregate_usage([row for run in report['onlineRuns'] for row in run['attempts']],
                                                     [event for run in report['onlineRuns'] for event in run['cacheEvents']])
            require(http.get('/api/admin/model-usage-summary', auth=('student', 'student123')).status_code == 403, 'Student can read admin usage')
        report['passed'] = True
    finally:
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        attempts = [item['ledger'] for item in report['rawUsageComparisons']] + [row for run in report['onlineRuns'] for row in run['attempts']]
        (output/'attempts.csv').write_text(attempts_csv(attempts), encoding='utf-8-sig')
        print(f"Report: {output}/report.json", flush=True)
    print('PASS model usage and cache acceptance', flush=True)


if __name__ == '__main__':
    main()
