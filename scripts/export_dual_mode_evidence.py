"""Export allowlisted acceptance metadata; never export profile configs/keys/replies."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-probe', required=True)
    parser.add_argument('--smoke', required=True)
    parser.add_argument('--deployment-checks', required=True, help='Dated operator record with source hashes; not inferred by this exporter')
    args = parser.parse_args()
    log = Path('target/dual-mode-offline-tests.log').read_text(encoding='utf-8')
    count = re.search(r'^Ran (\d+) tests', log, re.M)
    assert count and '\nOK\n' in log and 'Overall: PASS' in log
    api = json.loads(Path(args.api_probe).read_text(encoding='utf-8'))
    smoke = json.loads(Path(args.smoke).read_text(encoding='utf-8'))
    assert api['passed'] and smoke['passed']
    deployment = json.loads(Path(args.deployment_checks).read_text(encoding='utf-8'))
    for file, expected in deployment['sourceSha256'].items():
        assert hashlib.sha256(Path(file).read_bytes()).hexdigest() == expected, f'Stale deployment check: {file}'
    online = []
    with httpx.Client(base_url='http://127.0.0.1:8080', auth=('admin','admin123'), timeout=15, trust_env=False) as client:
        response = client.get('/api/admin/model-usage?limit=100'); response.raise_for_status()
        runs = response.json()
        for turn in smoke['turns']:
            run = next(r for r in runs if r.get('correlation',{}).get('request_id') == turn['requestId'])
            response = client.get('/api/admin/model-usage/'+run['runId']); response.raise_for_status()
            detail = response.json()
            assert detail['finalized'] and detail['ledgerWriteErrors'] == 0
            assert all(attempt['modelProfile']['mode'] == smoke['mode'] for attempt in detail['attempts'])
            online.append({'case':turn['case'], 'outcome':turn['outcome'], 'httpAttempts':detail['httpAttempts'],
                           'tokens':detail['tokens'], 'modelProfile':detail.get('modelProfile'),
                           'cacheLayers':detail['cacheLayers'], 'ledgerWriteErrors':detail['ledgerWriteErrors']})
        response = client.get('/api/admin/knowledge/status'); response.raise_for_status()
        knowledge = response.json()
        assert knowledge['vectorAvailable'] and knowledge['vectorChunks'] == knowledge['databaseChunks']
    tests = ast.parse(Path('tests/test_model_profiles.py').read_text(encoding='utf-8'))
    focused = sum(isinstance(node, ast.FunctionDef) and node.name.startswith('test_') for node in ast.walk(tests))
    shot = Path('docs/screenshots/model-settings-20261101.jpg')
    report = {
        'schemaVersion':1, 'generatedAt':datetime.now(timezone.utc).isoformat(),
        'codeReady':True, 'stage3FullyAccepted':False,
        'unitRegression':{'passed':True,'tests':int(count.group(1)),'focusedProfileTests':focused},
        'engineeringHarness':{'passed':True,'groups':len(re.findall(r'^\[PASS\]',log,re.M))},
        'realApiCapabilityProbe':{'passed':True,'requests':api['usage']['httpAttempts'],
                                  'tokens':api['usage']['tokens'],'capabilities':api['capabilities']},
        'realHttpSmoke':{'passed':True,'mode':smoke['mode'],'syntheticTurns':online},
        'knowledge':{name:knowledge[name] for name in ('databaseChunks','vectorChunks','embeddingModel')},
        'selfhostProtocolTests':{'usesMockModelResponses':True,'fullHarnessReplyReviewAndEmbeddingCovered':True,
                                'externalModelCallsBlockedInMockTransport':True,'realGpuTested':False},
        'deploymentChecks':deployment,
        'plannedGpu':{'gpu':'RTX 5090','vramGB':32,'recommendedSystemRamGB':64,'recommendedDataDiskGB':150,
                      'models':['Qwen/Qwen3-8B','Qwen/Qwen3-Embedding-0.6B']},
        'screenshot':{'file':str(shot),'bytes':shot.stat().st_size,'sha256':hashlib.sha256(shot.read_bytes()).hexdigest()},
        'remaining':['Real AutoDL model startup and SSH connection',
                     'Real GPU full local-mode run with external model APIs disabled',
                     'Two-account real GPU concurrency and same-budget quality/latency/throughput/cost benchmark'],
        'notice':'Mocks, declared context and SSE done are not model-quality or production-capacity evidence.',
    }
    output = Path('docs/benchmarks/dual-mode-code-20261101.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f'PASS public metadata export: {output}')


if __name__ == '__main__':
    main()
