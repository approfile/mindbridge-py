"""Two paid synthetic HTTP turns against the selected account model profile.

No profile/key changes. Logs only mode/revision and SSE outcomes, never replies.
This is a functional smoke check, not a quality or concurrency benchmark.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-mode', choices=['deployment_default', 'api', 'selfhost'], required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    user = os.environ.get('SMOKE_USERNAME', 'student')
    password = os.environ.get('SMOKE_PASSWORD', 'student123' if user == 'student' else '')
    if not password:
        raise SystemExit('Set SMOKE_PASSWORD privately for the selected account.')
    output = Path('target/model-profile-smoke') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output.mkdir(parents=True)
    report = {'passed':False, 'notice':'Functional smoke only; SSE done is not a quality judgment.', 'turns':[]}
    try:
        with httpx.Client(base_url=args.base_url, auth=(user,password), timeout=90, trust_env=False) as client:
            current = client.get('/api/model-profile'); current.raise_for_status()
            profile = current.json()
            if profile['mode'] != args.expect_mode:
                raise RuntimeError('Selected profile mode differs from --expect-mode. Save the intended profile in the product first.')
            report.update(mode=profile['mode'], revision=profile['revision'])
            status = client.get('/api/agent/status'); status.raise_for_status()
            status = status.json()
            if not status['realModelEnabled']:
                raise RuntimeError('The current account is still using mock.')
            report['chatModel'] = status['model']
            for case, message in [
                ('chat', '这是工程验收的虚构对话。请用一句话介绍如何安排今天的学习。'),
                ('consult', '这是工程验收的合成情境：我最近因为考试有些紧张，连续几晚睡不好，想找人聊两句。'),
            ]:
                started = time.monotonic()
                response = client.post('/api/chat/stream', json={'message':message})
                response.raise_for_status()
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                types = [event.get('type') for event in events]
                turn = {'case':case, 'requestId':response.headers.get('x-request-id'),
                        'outcome':'done' if 'done' in types else 'fallback_or_error',
                        'durationMs':round((time.monotonic()-started)*1000,3),
                        'visibleChars':sum(len(event.get('content') or '') for event in events if event.get('type') == 'token')}
                report['turns'].append(turn)
                if not turn['visibleChars']:
                    raise RuntimeError('No visible reply. Inspect the correlated server trace.')
                print(f"{case}: {turn['outcome']}; {turn['visibleChars']} visible characters", flush=True)
            if any(turn['outcome'] != 'done' for turn in report['turns']):
                raise RuntimeError('At least one turn fell back; inspect traces before accepting deployment.')
            latest = client.get('/api/model-profile'); latest.raise_for_status()
            if latest.json()['revision'] != profile['revision']:
                raise RuntimeError('Profile changed during the smoke run; results need separate attribution.')
            report['passed'] = True
    finally:
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Report: {output}/report.json', flush=True)


if __name__ == '__main__':
    main()
