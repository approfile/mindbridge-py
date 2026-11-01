"""Closed-loop HTTP benchmark against an owned loopback-only child server."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import threading
import time
import uuid

import httpx

from app.benchmark.protocol import load_dataset

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    temporary.replace(path)


def source_hashes():
    paths = sorted((ROOT/'app').rglob('*.py')) + [ROOT/'scripts/run_system_benchmark.py', ROOT/'app/benchmark/dataset-v1.json', ROOT/'app/pricing/model-prices-v1.json']
    return {str(path.relative_to(ROOT)).replace('\\','/'): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def isolated_environment(database, folder, variant, mock=False, configured=None):
    from app.core.config import Settings, get_settings
    from sqlalchemy.engine import make_url
    settings = configured or Settings()
    if not re.fullmatch(r'mindbridge_bench_[A-Za-z0-9_]+', database):
        raise ValueError('Use a dedicated mindbridge_bench_* database')
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'mysql' or database == url.database:
        raise ValueError('Benchmark requires an explicitly separate MySQL database')
    env = dict(os.environ)
    env.update(DATABASE_URL=url.set(database=database).render_as_string(hide_password=False),
               PRIVACY_KEY_FILE=str(folder/'private/privacy.key'),
               CHROMA_PERSIST_DIR=str(folder/'private/chroma'), CHROMA_SNAPSHOT_DIR=str(folder/'private/chroma-snapshots'),
               EXCEL_PATH=str(folder/'private/ledger.xlsx'), REDIS_URL='redis://redis:6379/14',
               OBSERVABILITY_LOG_FILE=str(folder/'private/application.jsonl'),
               BENCHMARK_NAMESPACE='mb-bench:'+folder.name+':'+variant, BENCHMARK_VARIANT=variant, BENCHMARK_OUTPUT=str(folder),
               SMTP_HOST='mailpit', SMTP_PORT='1025', SMTP_USERNAME='', SMTP_PASSWORD='',
               SMTP_USE_TLS='false', SMTP_USE_SSL='false', ALERT_EMAIL_DELIVERY_MODE='smtp',
               ALERT_EMAIL_FROM='benchmark@mindbridge.test', ALERT_EMAIL_TO='benchmark@mindbridge.test',
               ALERT_EMAIL_SUBJECT_PREFIX='[benchmark '+folder.name+']', ANONYMIZED_TELEMETRY='false')
    if mock:
        env = {k:v for k,v in env.items() if not k.startswith('AGENT_MODEL_')}
        env.update(AI_PROVIDER='mock', KNOWLEDGE_VECTOR_ENABLED='false', KNOWLEDGE_VECTOR_REQUIRED='false')
    else:
        env.update(KNOWLEDGE_VECTOR_ENABLED='true', KNOWLEDGE_VECTOR_REQUIRED='true')
    # The runner itself reads evidence from the same isolated storage. Never
    # change the parent app's environment, which is a separate process.
    os.environ.update({key: value for key, value in env.items() if key in Settings.model_fields or key.lower() in Settings.model_fields})
    get_settings.cache_clear()
    return env


def wait_ready(client, process, timeout=120):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Benchmark child exited; inspect private/server.log')
        try:
            if client.get('/actuator/health').json().get('status') == 'UP':
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(.2)
    raise TimeoutError('Benchmark child startup timeout')


def wait_idle(client):
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        if client.get('/__benchmark/runtime').json()['active'] == 0:
            return
        time.sleep(.1)
    raise TimeoutError('Admission slots not released')


def seeded_session(factory, user_id, pairs):
    from app.models.entities import ChatSession, ChatMessage
    with factory() as db:
        session = ChatSession(user_id=user_id, public_id=uuid.uuid4().hex, title='Synthetic long-history benchmark')
        db.add(session); db.flush()
        for index in range(pairs):
            fact = '我的项目代号是银杏。' if index == 0 else '我已经把项目代号改成蓝桥。' if index == pairs-1 else f'第{index}次学习记录。'
            db.add(ChatMessage(user_id=user_id, session_id=session.id, role='USER', content=fact+'我在学习 Python，练习列表、函数和基本测试。'*28))
            db.add(ChatMessage(user_id=user_id, session_id=session.id, role='ASSISTANT', content='这是虚构的学习历史记录，可以按计划逐步练习。'*20))
        db.commit()
        return session.public_id


def request_turn(client, case, auth, message, session_id=None, barrier=None):
    row = {'caseId': case['id'], 'visibleText': '', 'sseEvents': [], 'httpStatus': None,
           'clientFirstVisibleTokenMs': None, 'errorType': None, 'requestId': None,'sampleTraceId':uuid.uuid4().hex}
    payload = {'message': message}
    if session_id: payload['sessionId'] = session_id
    if barrier: barrier.wait(timeout=15)
    start = time.perf_counter()
    row['clientStart'] = start
    row['startedAt'] = datetime.now(timezone.utc).isoformat()
    try:
        with client.stream('POST', '/api/chat/stream', auth=auth, json=payload,
                           headers={'X-Benchmark-Case': case['id'],'X-Benchmark-Sample':row['sampleTraceId']}) as response:
            row['httpStatus'] = response.status_code
            row['requestId'] = response.headers.get('x-request-id')
            if response.status_code != 200:
                row['httpErrorCode'] = json.loads(response.read()).get('code')
            else:
                event = None
                for line in response.iter_lines():
                    if line.startswith('event: '): event = line[7:]
                    elif line.startswith('data: '):
                        data = json.loads(line[6:]); row['sseEvents'].append(event)
                        if data.get('sessionId'): row['sessionId'] = data['sessionId']
                        if event == 'token' and data.get('content'):
                            if row['clientFirstVisibleTokenMs'] is None:
                                row['clientFirstVisibleTokenMs'] = (time.perf_counter()-start)*1000
                            row['visibleText'] += data['content']
    except Exception as error:
        row['errorType'] = type(error).__name__
    row['clientEnd'] = time.perf_counter()
    row['clientEndToEndMs'] = (row['clientEnd']-start)*1000
    return row


def collect_evidence(row, case, factory):
    from app.models.entities import ModelUsageRun, AgentRunTrace, ChatMessage, ToolJob
    from app.services.usage_reporting import usage_run_detail
    if row['httpStatus'] != 200:
        row.update(usage=None, mechanicalChecks={'admissionRejected': row['httpStatus'] in {409,429}}, retrievedContexts=[])
        return row
    deadline = time.monotonic()+15
    detail = None
    while time.monotonic() < deadline:
        with factory() as db:
            # Bounded benchmark database only; never scans the business DB.
            runs = db.query(ModelUsageRun).filter_by(scope='online_chat').order_by(ModelUsageRun.created_at.desc()).limit(250).all()
            run = next((run for run in runs if json.loads(run.payload_json).get('correlation',{}).get('request_id') == row['requestId']), None)
            if run and run.finalized:
                detail = usage_run_detail(db, run.id); break
            if run is None and 'meta' not in row['sseEvents'] and 'error' in row['sseEvents']:
                break  # Pre-planning failure can have no model/cache ledger.
        time.sleep(.1)
    row['usage'] = detail
    from app.core.config import get_settings
    diagnostics = Path(get_settings().privacy_key_file).parent/'diagnostics'/(row['sampleTraceId']+'.json')
    row['diagnostics'] = json.loads(diagnostics.read_text()) if diagnostics.exists() else []
    checks = {'finalizedLedger': bool(detail and detail['finalized'])}
    row['retrievedContexts'] = []
    if detail and detail['correlation'].get('trace_id'):
        with factory() as db:
            trace = db.get(AgentRunTrace, detail['correlation']['trace_id'])
            steps = json.loads(trace.agent_steps_json)
            outcome = next((x for x in reversed(steps) if x.get('kind') == 'response_outcome'), {})
            observation = next((x['data'] for x in steps if x.get('kind') == 'observability'), {})
            row.update(intent=trace.intent, risk=trace.risk_level, responseOutcome=outcome,
                       planAccepted=any(x.get('type') == 'PLAN_ACCEPTED' for x in steps),
                       groundingReviews=[{k:x.get(k) for k in ['groundingRequired','groundingStatus','approved']} for x in steps if x.get('kind') == 'response_review'])
            row['retrievedContexts'] = json.loads(trace.retrieved_knowledge_json)
            reply = db.query(ChatMessage).filter_by(session_id=trace.session_id, role='ASSISTANT').order_by(ChatMessage.id.desc()).first()
            checks.update(intentMatches=trace.intent == case['intent'], riskMatches=trace.risk_level == case['risk'],
                          assistantPersisted=bool(reply and reply.content == row['visibleText']),
                          reportPolicy=(trace.report_id is None) if case['intent'] == 'CHAT' else trace.report_id is not None)
            if case.get('emptyRetrieval'):
                checks['emptyContextInterventionReached'] = any(x['stage'] == 'benchmark.empty_retrieval' for x in observation.get('spans',[]))
            report_id = trace.report_id
        # Check actual durable execution, with a separate, bounded wait. This
        # wait is excluded from each HTTP latency, but can pace closed-loop load.
        if report_id:
            until = time.monotonic()+20
            while True:
                with factory() as db:
                    jobs = db.query(ToolJob).filter_by(report_id=report_id).all()
                    states = {job.kind: job.status for job in jobs}
                if jobs and all(status == 'SUCCESS' for status in states.values()): break
                if time.monotonic() >= until: break
                time.sleep(.1)
            row['toolStates'] = states
            expected = {'EXCEL_REPORT','CASE_CREATE','ALERT_SEND'} if case['risk'] == 'HIGH' else {'EXCEL_REPORT'}
            checks['durableEffectsCompleted'] = expected <= states.keys() and all(states[k] == 'SUCCESS' for k in expected)
        if case.get('mustContain'):
            checks['memoryFactCorrect'] = case['mustContain'] in row['visibleText']
        if case.get('expectedFallback'):
            checks['expectedReplyMode'] = detail['streamStatus'] == 'safe_fallback' and bool(row['visibleText']) and 'done' not in row['sseEvents']
        else:
            checks['expectedReplyMode'] = detail['streamStatus'] == 'accepted' and 'done' in row['sseEvents']
        if case.get('fault'):
            injected = [x for x in detail['attempts'] if x.get('injectedFault')]
            checks['faultReached'] = len(injected) == (2 if case.get('expectedFallback') else 1)
            checks['boundedRetry'] = detail['retryAttempts'] >= 1 and max(x.get('attemptNumber',1) for x in detail['attempts']) <= 2
    else:
        checks['traceAvailable'] = False
    row['mechanicalChecks'] = checks
    return row


def run_scenario(client, entry, case, account, factory, barrier=None):
    session = seeded_session(factory, account['id'], case['seedHistoryPairs']) if case.get('seedHistoryPairs') else None
    turns = []
    for index, message in enumerate(case['turns']):
        row = request_turn(client, case, (account['username'], account['password']), message, session, barrier if index == 0 else None)
        row.update(turnIndex=index, observationLabel='first_observed' if index == 0 else 'same_session_repeat', userId=account['id'])
        session = row.get('sessionId', session)
        turns.append(collect_evidence(row, case, factory))
    return {**entry, 'turns': turns}


def run_benchmark(args):
    from app.core.config import Settings
    from app.services.agent_models import AgentModelRegistry
    configured = Settings()
    dataset, digest = load_dataset()
    folder = (ROOT/args.output).resolve()
    if not folder.is_relative_to(ROOT/'target/system-benchmark'):
        raise ValueError('Output must stay in target/system-benchmark')
    folder.mkdir(parents=True, exist_ok=False)
    (folder/'private').mkdir(mode=0o700)
    variants = ['event_driven', 'sequential']
    nominal = [dataset['nominal'][0], dataset['nominal'][2]] if args.smoke else dataset['nominal']
    levels, repeats = ([1], 1) if args.smoke else ([1,2,4], 2)
    rng = random.Random(20261101)
    plan = []
    for variant in variants:
        for concurrency in levels:
            entries = [{'variant':variant, 'concurrency':concurrency, 'trial':trial, 'caseId':case['id'], 'layer':'nominal' if not case.get('emptyRetrieval') else 'context_intervention'}
                       for trial in range(1,repeats+1) for case in nominal]
            rng.shuffle(entries); plan.extend(entries)
        plan.extend({'variant':variant,'concurrency':1,'trial':1,'caseId':case['id'],'layer':'injected_fault'} for case in dataset['controlled'])
        plan.extend({'variant':variant,'concurrency':5,'trial':1,'caseId':'capacity-probe','layer':'admission_probe','probeIndex':n} for n in range(5))
    for number,entry in enumerate(plan):
        entry['sampleId'] = f'S{number+1:04d}'
    profiles = {role: vars(AgentModelRegistry(configured).profile_for(role)) for role in ['UnderstandingAgent','SafetyAgent','ContextAgent','ResponseAgent']}
    if not args.mock and (configured.ai_provider != 'openai' or any(p['provider']!='openai' for p in profiles.values())):
        raise ValueError('Real protocol requires the configured OpenAI-compatible model profiles')
    manifest = {'protocol':'system-baseline-v1', 'createdAt':datetime.now(timezone.utc).isoformat(), 'datasetSha256':digest,
                'datasetVersion':dataset['version'], 'smoke':args.smoke, 'mock':args.mock, 'randomSeed':20261101,
                'database':args.database, 'redisDatabase':14, 'namespaces':'mb-bench:'+folder.name+':<variant>',
                'modelProfiles':profiles, 'embeddingModel':configured.openai_embedding_model,
                'limits':{'global':configured.turn_max_inflight,'perUser':configured.turn_max_inflight_per_user,
                          'turnSeconds':configured.turn_timeout_seconds, 'modelTransportAttempts':configured.model_transport_max_attempts},
                'sourceHashes':source_hashes(), 'plannedTrials':plan,
                'qualityOrigin':dataset['origin'], 'judgeBias':'separate requests/prompt, same configured provider/model; no human/expert validation',
                'orderNotice':'event-driven first; shared cloud cache and sequential execution can bias comparison; no causal speedup claim'}
    from importlib.metadata import version
    from urllib.parse import urlsplit
    manifest['environment'] = {'python':sys.version.split()[0],'platform':sys.platform,'visibleCpuCount':os.cpu_count(),
                               'libraries':{name:version(name) for name in ['fastapi','starlette','httpx','SQLAlchemy','redis','chromadb']}}
    manifest['connections'] = {'generationHost':urlsplit(configured.openai_base_url).hostname,
                               'embeddingHost':urlsplit(configured.openai_embedding_base_url).hostname}
    manifest['settings'] = {name:getattr(configured,name) for name in [
        'ai_temperature','ai_max_tokens','openai_thinking_mode','knowledge_top_k','knowledge_candidate_k',
        'knowledge_chunk_size','knowledge_chunk_overlap','knowledge_hybrid_vector_weight','knowledge_hybrid_bm25_weight',
        'knowledge_rerank_enabled','memory_compaction_enabled','memory_compaction_recent_messages','memory_summary_max_chars',
        'chat_history_limit','agent_max_rounds','reply_timeout_seconds','reply_review_max_tokens','reply_max_revisions',
        'model_stream_include_usage','model_retry_base_delay_seconds']}
    manifest['corpusHashes'] = {path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT/'app/knowledge').glob('*.md'))}
    if configured.turn_max_inflight != 4 or configured.model_transport_max_attempts != 2:
        raise ValueError('Protocol requires current four-slot, two-attempt configuration')
    save(folder/'plan.json', manifest)  # Must exist before the first generation.
    observations = []
    try:
        for variant in variants:
            env = isolated_environment(args.database, folder, variant, args.mock, configured)
            from app.core.database import SessionLocal, engine
            from sqlalchemy import inspect
            if variant == variants[0] and inspect(engine).get_table_names():
                raise ValueError('Choose a fresh empty benchmark database; no implicit deletion')
            process = None
            with (folder/'private'/f'{variant}-server.log').open('w',encoding='utf-8') as log, httpx.Client(base_url='http://127.0.0.1:18081', timeout=90, trust_env=False, limits=httpx.Limits(max_connections=8)) as client:
                try:
                    process = subprocess.Popen([sys.executable,'-m','uvicorn','app.benchmark.server:create_benchmark_app','--factory',
                                                '--host','127.0.0.1','--port','18081','--no-access-log'], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
                    wait_ready(client, process)
                    from app.core.security import hash_password
                    from app.models.entities import UserAccount
                    accounts = []
                    with SessionLocal() as db:
                        for index in range(5):
                            password = uuid.uuid4().hex
                            user = UserAccount(username=f'bench_{variant}_{index}', display_name='Synthetic Benchmark', password_hash=hash_password(password))
                            db.add(user); db.flush()
                            accounts.append({'id':user.id,'username':user.username,'password':password})
                        db.commit()
                    lookup = {c['id']:c for c in dataset['nominal']+dataset['controlled']}
                    lookup['capacity-probe'] = {**dataset['nominal'][0],'id':'capacity-probe'}
                    for concurrency in levels + [0,5]:
                        entries = [e for e in plan if e['variant']==variant and (e['layer']=='injected_fault' if concurrency == 0 else e['layer']=='admission_probe' if concurrency==5 else e['concurrency']==concurrency and e['layer'] in {'nominal','context_intervention'})]
                        workers = concurrency or 1
                        wait_idle(client)
                        barrier = threading.Barrier(5) if concurrency == 5 else None
                        # One account per worker prevents the per-user limit
                        # from masquerading as a global concurrency bottleneck.
                        buckets = [entries[index::workers] for index in range(workers)]
                        def worker(index):
                            result = []
                            for entry in buckets[index]:
                                result.append(run_scenario(client, entry, lookup[entry['caseId']], accounts[index], SessionLocal, barrier))
                                print(f'CASE {variant} c={workers} {entry["caseId"]} trial={entry["trial"]}', flush=True)
                            return result
                        with ThreadPoolExecutor(max_workers=workers) as pool:
                            for future in as_completed([pool.submit(worker, n) for n in range(workers)]):
                                observations.extend(future.result())
                                save(folder/'private/observations.json', observations)
                        wait_idle(client)
                        print(f'CAPTURED {variant} concurrency={workers} layer={entries[0]["layer"]} scenarios={len(entries)}', flush=True)
                finally:
                    if process is not None:
                        process.terminate()
                        try: process.wait(timeout=20)
                        except subprocess.TimeoutExpired: process.kill(); process.wait()
        if {x['sampleId'] for x in observations} != {x['sampleId'] for x in plan} or len(observations)!=len(plan):
            raise RuntimeError('Missing or duplicate planned benchmark observations')
        if manifest['sourceHashes'] != source_hashes():
            raise RuntimeError('Execution source changed during frozen benchmark')
        from app.benchmark.judge import judge_observations
        judgments = [] if args.mock else judge_observations(observations, dataset, folder, SessionLocal, configured)
        save(folder/'private/judgments.json', judgments)
        if manifest['sourceHashes'] != source_hashes():
            raise RuntimeError('Execution source changed during offline judging')
        from app.benchmark.report import write_report
        report = write_report(folder, manifest, observations, judgments, SessionLocal)
        print('FINISHED '+str(folder/'SUMMARY.md')+'; captured all planned trials; quality failures retained', flush=True)
        return report
    except BaseException as error:
        save(folder/'collector-error.json', {'errorType':type(error).__name__,'capturedScenarios':len(observations)})
        raise
