"""Isolated child-process execution and fact collection for Agent eval step 3.

Only the controlled model profile is implemented here. These are real HTTP,
SQL, Redis, Chroma, Excel and local SMTP operations, not model-quality scores.
No production service module is imported before the isolated config is exported.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime,timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from unittest.mock import patch

from app.evaluation.agent_result_evidence import (EventRecorder,controlled_embedding,read_ledger,read_mailpit,identify_reply)

ROOT=Path(__file__).resolve().parents[2]


def configure(folder,task,nonce):
    from app.core.config import Settings,get_settings
    folder=folder.resolve()
    if not folder.is_relative_to((ROOT/'.local-runtime/agent-results-20261027').resolve()):
        raise ValueError('Evaluation state must stay in its dedicated private directory')
    folder.mkdir(parents=True,exist_ok=False);os.chmod(folder,0o700)
    database_url=f'sqlite:///{folder/"evaluation.sqlite3"}'
    values=dict(database_url=database_url,ai_provider='mock',agent_model_default_provider='mock',agent_model_default_model='mock',
      openai_api_key='',openai_embedding_api_key='controlled-local-only',openai_embedding_model='controlled-lexical-hash-v1',
      knowledge_vector_enabled=True,knowledge_vector_required=True,chroma_persist_dir=str(folder/'chroma'),
      chroma_snapshot_dir=str(folder/'snapshots'),chroma_snapshot_keep=1,privacy_key_file=str(folder/'privacy.key'),
      redis_url='redis://127.0.0.1:16379/15',excel_path=str(folder/'ledger.xlsx'),observability_log_file=str(folder/'events.jsonl'),
      alert_email_delivery_mode='smtp',smtp_host='127.0.0.1',smtp_port=1025,smtp_use_ssl=False,smtp_use_tls=False,
      smtp_username='',smtp_password='',alert_email_from='agent-eval@example.test',alert_email_to=f'agent-eval-{nonce}@example.test',
      alert_email_subject_prefix=f'[MB-EVAL:{nonce}]',tool_queue_enabled=True,tool_queue_poll_interval_seconds=.05,
      tool_queue_retry_delay_seconds=1.,turn_timeout_seconds=60.)
    values.update(task['environment']['configurationOverrides']);s=Settings(_env_file=None,**values)
    for role in ('coordinator','understanding','safety','context','response'):
        setattr(s,f'agent_model_{role}_provider','mock');setattr(s,f'agent_model_{role}_model','mock')
    for k,v in s.model_dump().items():os.environ[k.upper()]=str(v).lower() if isinstance(v,bool) else str(v)
    os.environ['ANONYMIZED_TELEMETRY']='false';get_settings.cache_clear()
    return s


def seed(s,task,nonce):
    from app.core.database import Base,engine,SessionLocal
    from app.core.privacy_crypto import cipher
    from app.core.security import hash_password
    from app.models.entities import UserAccount,ChatSession,ChatMessage,PsychologicalReport,RiskCase,ToolJob,AgentRunTrace
    from app.services.knowledge import KnowledgeService
    cipher(s);Base.metadata.create_all(engine)
    if engine.dialect.name=='sqlite':
        with engine.connect() as c:c.exec_driver_sql('PRAGMA journal_mode=WAL')
    actors={};sessions={};reports={};cases={};jobs={};traces={}
    with SessionLocal() as db:
        for alias in task['initialState']['actorAliases']:
            roles=['ROLE_USER','ROLE_ADMIN'] if alias=='admin_plain' else ['ROLE_USER']
            u=UserAccount(username=alias,display_name='Synthetic '+alias,password_hash=hash_password('isolated-eval-password'),roles_csv=','.join(roles))
            db.add(u);db.flush();actors[alias]=u.id
        for f in task['initialState']['sessions']:
            session=ChatSession(user_id=actors[f['owner']],public_id=f'eval-{nonce}-{f["alias"]}',title='Synthetic evaluation')
            db.add(session);db.flush();sessions[f['alias']]={'id':session.id,'publicId':session.public_id,'ownerId':actors[f['owner']]}
            for m in f['history']:db.add(ChatMessage(session_id=session.id,user_id=session.user_id,role=m['role'].upper(),content=m['content']))
        for f in task['initialState']['reports']:
            ref=sessions[f['session']];r=PsychologicalReport(user_id=ref['ownerId'],session_id=ref['id'],content='seed private record '+nonce,
              intent='RISK' if f['risk']=='HIGH' else 'CONSULT',emotion='HIGH_RISK' if f['risk']=='HIGH' else 'ANXIETY',
              emotion_score=4. if f['risk']=='HIGH' else 3.1,risk_level=f['risk'],confidence=None,summary='Synthetic seeded state')
            db.add(r);db.flush();reports[f['alias']]=r.id
            if f['caseStatus']:
                case=RiskCase(report_id=r.id,risk_level=r.risk_level,status=f['caseStatus'],owner='admin_plain',summary='Synthetic case',handoff_summary='Synthetic case handoff')
                db.add(case);db.flush();cases[f['alias']]=case.id
            trace=AgentRunTrace(user_id=r.user_id,session_id=r.session_id,report_id=r.id,intent=r.intent,risk_level=r.risk_level,
              original_input='seed protected trace '+nonce,sanitized_input='synthetic fixture',agent_steps_json='[]')
            db.add(trace);db.flush();traces[f['alias']]=trace.id
        for f in task['initialState']['jobs']:
            job=ToolJob(report_id=reports[f['report']],kind=f['kind'],status=f['state'],attempts=0,max_attempts=3,last_error='',
              idempotency_key=f'{reports[f["report"]]}:{f["kind"]}',claim_token=f['claimToken'],
              lease_expires_at=datetime.utcnow()+timedelta(seconds=90 if f['lease']=='future' else -1))
            db.add(job);db.flush();jobs[f['alias']]=job.id
        db.commit()
        knowledge=KnowledgeService(db,s)
        for source in sorted((ROOT/'app/knowledge').glob('*.md')):knowledge.ensure_source(source.name,source.read_text())
        if knowledge.vector_store.count()!=34:raise RuntimeError('Frozen corpus did not index to 34 chunks')
    return {'actors':actors,'sessions':sessions,'reports':reports,'cases':cases,'jobs':jobs,'traces':traces}


def install_hooks(stack,s,rec):
    from app.agents.events import CollaborationBlackboard
    from app.agents.coordinator import EventDrivenCoordinator
    from app.agents.harness import MindBridgeAgentHarness
    from app.services.ai import AiClient
    from app.services.knowledge import KnowledgeService,SearchResult
    from app.services.vector_store import ChromaKnowledgeStore
    from app.services.response_review import ReplyReviewPipeline
    from app.services.turn_work import OwnedTurnWork
    from app.services.turn_budget import TurnControl
    from app.services.tool_execution import ToolExecutionService
    from app.services.tools import ToolOrchestrationService
    from app.core.database import SessionLocal
    from app.models.entities import ToolJob,ToolAuditRecord,AgentRunTrace,PsychologicalReport
    import httpx
    original_add=CollaborationBlackboard.add_artifact
    def add(board,artifact):
        result=original_add(board,artifact)
        if rec.active:
            t=rec.turns[rec.active];t['runId']=board.turn_id
            if artifact.kind=='risk':t['route']['risk']=artifact.payload['risk'];rec.event('RISK_ASSESSED')
            if artifact.kind=='intent':t['route']['intent']=artifact.payload['intent']
            if artifact.kind=='context':t['context'].update({'queryRewrite':artifact.payload.get('queryRewrite',{})})
            if artifact.kind=='response_proposal':t['context']['evidenceMode']=artifact.payload.get('evidenceMode')
        return result
    stack.enter_context(patch.object(CollaborationBlackboard,'add_artifact',add))
    original_accept=CollaborationBlackboard.accept_plan
    def accept(board,*args,**kwargs):
        result=original_accept(board,*args,**kwargs)
        if rec.active:
            metadata=kwargs.get('metadata') or (args[3] if len(args)>3 else {})
            rec.turns[rec.active]['planChecks']=metadata.get('checks',{});rec.event('PLAN_ACCEPTED')
        return result
    stack.enter_context(patch.object(CollaborationBlackboard,'accept_plan',accept))
    original_coordinator=EventDrivenCoordinator.run
    def coordinate(obj,board):
        if rec.has('limit_agent_rounds'):rec.trigger('limit_agent_rounds')
        return original_coordinator(obj,board)
    stack.enter_context(patch.object(EventDrivenCoordinator,'run',coordinate))
    original_complete=AiClient.complete
    def complete(client,messages,**kwargs):
        if any('把学生输入改写' in m.content for m in messages) and rec.has('raise_timeout','query.rewrite'):
            rec.trigger('raise_timeout','query.rewrite');raise httpx.ReadTimeout('controlled rewrite timeout')
        return original_complete(client,messages,**kwargs)
    stack.enter_context(patch.object(AiClient,'complete',complete))
    original_vector=KnowledgeService._retrieve_vector
    def query_vector(service,*args,**kwargs):
        rec.thread.query=True
        try:return original_vector(service,*args,**kwargs)
        finally:rec.thread.query=False
    stack.enter_context(patch.object(KnowledgeService,'_retrieve_vector',query_vector))
    def embed(store,texts):
        rec.embedding_calls+=1
        if getattr(rec.thread,'query',False) and rec.has('raise_timeout','embedding.request'):
            rec.trigger('raise_timeout','embedding.request');raise httpx.ReadTimeout('controlled embedding timeout')
        return [controlled_embedding(t) for t in texts]
    stack.enter_context(patch.object(ChromaKnowledgeStore,'_embed',embed))
    original_retrieve=KnowledgeService.retrieve
    def retrieve(service,*args,**kwargs):
        f=rec.has('delay_until_deadline')
        if f and rec.active and rec.turns[rec.active]['route'].get('risk')=='HIGH':
            rec.trigger('delay_until_deadline');time.sleep(f['parameters']['delayMilliseconds']/1000)
        result=original_retrieve(service,*args,**kwargs)
        f=rec.has('replace_retrieval')
        if f:
            rec.trigger('replace_retrieval')
            return [SearchResult(None,c['source'],c['content'],1.) for c in f['parameters']['chunks']]
        return result
    stack.enter_context(patch.object(KnowledgeService,'retrieve',retrieve))
    original_gen=ReplyReviewPipeline._generate_candidate
    def committed_outbox_exists():
        if not rec.active or not rec.turns[rec.active]['runId']:return False
        with SessionLocal() as db:
            return db.query(ToolJob.id).join(PsychologicalReport,ToolJob.report_id==PsychologicalReport.id).filter(
                ToolJob.correlation_json.contains(rec.turns[rec.active]['runId'])).first() is not None
    async def generate(pipeline,*args,**kwargs):
        rec.event('GENERATION_STARTED')
        if rec.has('raise_transport_error','reply.generate') and committed_outbox_exists():
            rec.trigger('raise_transport_error','reply.generate');raise ConnectionError('controlled generation outage')
        result=await original_gen(pipeline,*args,**kwargs)
        rec.turns[rec.active].setdefault('candidates',{})[hashlib.sha256(result[0].encode()).hexdigest()]=result[0]
        return result
    stack.enter_context(patch.object(ReplyReviewPipeline,'_generate_candidate',generate))
    original_review=ReplyReviewPipeline._review_candidate
    async def review(pipeline,*args,**kwargs):
        f=rec.has('replace_model_response','reply.review')
        if f and rec.turns[rec.active].get('candidates'):
            rec.trigger('replace_model_response','reply.review');return f['parameters']['body']
        return await original_review(pipeline,*args,**kwargs)
    stack.enter_context(patch.object(ReplyReviewPipeline,'_review_candidate',review))
    original_run=ReplyReviewPipeline.run
    async def run(pipeline,outcome):
        if rec.has('client_disconnect'):await asyncio.sleep(.5)
        return await original_run(pipeline,outcome)
    stack.enter_context(patch.object(ReplyReviewPipeline,'run',run))
    original_persist=OwnedTurnWork.persist_reply
    def persist(work,reply):
        result=original_persist(work,reply)
        # Observe the committed SQL review, not AiClient's raw approval text.
        with SessionLocal() as db:
            trace=db.get(AgentRunTrace,work.outcome.trace_id)
            for r in json.loads(trace.agent_steps_json):
                if r.get('kind')=='response_review':
                    rec.event('BODY_REVIEW',approved=r['approved'],candidateSha256=r['candidateSha256'])
            rec.turns[rec.active]['bodyCaptured']=True
        return result
    stack.enter_context(patch.object(OwnedTurnWork,'persist_reply',persist))
    original_stop=TurnControl.stop
    def stop(control,reason):
        first=control.reason is None;result=original_stop(control,reason)
        if first and reason in {'TURN_TIMEOUT','CLIENT_DISCONNECTED'}:
            rec.event(reason)
            if reason=='CLIENT_DISCONNECTED' and rec.has('client_disconnect'):rec.trigger('client_disconnect')
        return result
    stack.enter_context(patch.object(TurnControl,'stop',stop))
    original_execute=ToolExecutionService.execute
    def execute(service,tool_name,*args,**kwargs):
        job_id=kwargs.get('job_id');report_id=kwargs.get('report_id');runid=None
        if job_id:
            with SessionLocal() as db:
                job=db.get(ToolJob,job_id)
                if job:runid=json.loads(job.correlation_json or '{}').get('run_id')
        row={'seq':rec.tick(),'jobId':str(job_id or 'probe'),'kind':tool_name,
          'reportId':str(report_id),'runId':runid,'turnId':rec.active,
          'argumentsDigest':hashlib.sha256(json.dumps({'tool':tool_name,'reportId':report_id,'jobId':job_id},sort_keys=True).encode()).hexdigest(),
          'origin':'workflow' if rec.task['executionScope']=='conversation' else 'probe','outcome':'UNKNOWN'}
        rec.calls.append(row)
        try:
            result=original_execute(service,tool_name,*args,**kwargs)
            with SessionLocal() as db:
                audit=db.query(ToolAuditRecord).filter_by(job_id=job_id,report_id=report_id,tool_name=tool_name).order_by(ToolAuditRecord.id.desc()).first()
                row['outcome']='SUCCESS' if audit and audit.status=='SUCCESS' else 'UNKNOWN'
            return result
        except Exception:
            row['outcome']='FAILED';raise
        finally:row['completedSeq']=rec.tick()
    stack.enter_context(patch.object(ToolExecutionService,'execute',execute))
    original_send=ToolOrchestrationService._send_alert_email;send_count=[0]
    def send(tools,*args,**kwargs):
        f=rec.has('raise_before_side_effect','tool.ALERT_SEND');send_count[0]+=1
        if f and (f['parameters']['times']=='all' or send_count[0]<=f['parameters']['times']):
            rec.trigger('raise_before_side_effect','tool.ALERT_SEND');raise ConnectionError('controlled SMTP pre-send fault')
        if tools.settings.smtp_host!='127.0.0.1' or tools.settings.smtp_port!=1025:raise RuntimeError('SMTP must stay in local capture sink')
        return original_send(tools,*args,**kwargs)
    stack.enter_context(patch.object(ToolOrchestrationService,'_send_alert_email',send))


def make_app(s):
    from fastapi import FastAPI
    from app.api.routes import router
    from app.services.turn_execution import TurnRuntime
    from app.services.turn_middleware import TurnBudgetMiddleware
    from app.services.observability import ObservationMiddleware,configure_logging
    configure_logging(s)
    app=FastAPI();runtime=TurnRuntime(s);app.state.turn_runtime=runtime
    app.include_router(router)
    app.add_middleware(TurnBudgetMiddleware,settings=s,runtime=runtime);app.add_middleware(ObservationMiddleware)
    # No production startup: in particular, never scan/migrate other Redis keys.
    return app,runtime


def wait_runtime(runtime,seconds=15):
    until=time.monotonic()+seconds
    while runtime.stats()['active'] and time.monotonic()<until:time.sleep(.01)
    return runtime.stats()['active']==0


def wait_jobs(s,seconds):
    from app.core.database import SessionLocal
    from app.models.entities import ToolJob
    end=time.monotonic()+seconds;started=time.perf_counter()
    while time.monotonic()<end:
        with SessionLocal() as db:
            if not db.query(ToolJob.id).filter(ToolJob.status.in_(['PENDING','RUNNING'])).first():return True,(time.perf_counter()-started)*1000
        time.sleep(.04)
    return False,(time.perf_counter()-started)*1000


async def send_turn(base,inp,aliases,rec):
    import httpx
    meta={};lines=[];event='';client_error=None
    started=time.perf_counter()
    async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=75) as client:
        try:
            async with client.stream('POST','/api/chat/stream',auth=(inp['actor'],'isolated-eval-password'),
               json={'message':inp['input'],'sessionId':aliases['sessions'][inp['session']]['publicId']}) as response:
                status=response.status_code
                async for line in response.aiter_lines():
                    if line.startswith('event: '):event=line[7:]
                    elif line.startswith('data: '):
                        data=json.loads(line[6:]);lines.append({'event':event,'data':data})
                        if event=='meta':
                            meta=data
                            if rec.has('client_disconnect'):break
                        if event in {'token','done','error'}:
                            r=rec.event({'token':'SSE_TOKEN','done':'SSE_DONE','error':'SSE_ERROR'}[event])
                            if event=='token':
                                r['_visibleToken']=True
                                if rec.turns[rec.active]['firstTextMs'] is None:rec.turns[rec.active]['firstTextMs']=(time.perf_counter()-started)*1000
        except Exception as e:client_error=type(e).__name__;status=0
    rec.turns[rec.active]['sse']=lines
    rec.turns[rec.active]['clientStatus']=status
    rec.turns[rec.active]['clientError']=client_error
    rec.turns[rec.active]['turnMs']=(time.perf_counter()-started)*1000
    return meta


def seed_snapshot(aliases):
    from app.core.database import SessionLocal
    from app.models.entities import PsychologicalReport,RiskCase,CaseNote
    with SessionLocal() as db:
        return {'reports':{a:{'id':r.id,'risk':r.risk_level,'contentHash':hashlib.sha256(r.content.encode()).hexdigest()}
                  for a,id in aliases['reports'].items() if (r:=db.get(PsychologicalReport,id))},
                'cases':{a:db.get(RiskCase,id).status for a,id in aliases['cases'].items()},'caseNotes':db.query(CaseNote).count()}


def run_boundaries(base,task,aliases,s):
    import httpx
    from app.core.database import SessionLocal
    from app.models.entities import ToolAuditRecord,CaseNote,AlertRecord,DataAccessAudit
    from app.services.tool_execution import ToolExecutionService
    results=[];raw=[]
    for action in task['boundaryActions']:
        args=action['arguments'];before=seed_snapshot(aliases);denied=False;http_status=None;accepted=None
        with SessionLocal() as db:before_audits=db.query(ToolAuditRecord).count();before_reads=db.query(DataAccessAudit).count()
        if action['kind']=='call_tool':
            kw={}
            for name,key in [('reportRef','report_id'),('caseRef','case_id'),('jobRef','job_id')]:
                if name in args:
                    alias=args[name].split('.')[0];kw[key]=aliases[{'reportRef':'reports','caseRef':'cases','jobRef':'jobs'}[name]][alias]
            if 'actorRef' in args:kw['actor']=args['actorRef']
            if 'claimedActorUsername' in args:kw['actor']=args['claimedActorUsername']
            if 'claimToken' in args:kw['claim_token']=args['claimToken']
            if 'note' in args:kw['note']=args['note']
            with SessionLocal() as db:
                try:ToolExecutionService(db,__import__('app.core.config',fromlist=['get_settings']).get_settings()).execute(action['target'],**kw)
                except Exception:denied=True
        elif action['kind']=='http_request':
            path=action['target'].split(' ',1)[1]
            if 'reportRef' in args:path=path.replace('{reportRef}',str(aliases['reports'][args['reportRef']]))
            if 'traceRef' in args:path=path.replace('{traceRef}',str(aliases['traces'][args['traceRef'].split('.')[0]]))
            r=httpx.get(base+path,auth=(action['actor'],'isolated-eval-password'),headers={'X-Access-Purpose':args.get('accessPurpose','case_followup')},trust_env=False,timeout=10)
            http_status=r.status_code;denied=http_status in {400,401,403,404};raw.append({'actionId':action['id'],'status':http_status,'body':r.json()})
        else:
            from app.agents.events import CollaborationBlackboard,AgentArtifact
            from app.agents.plan_validation import validate_plan
            from app.schemas.dtos import AiMessage
            from app.services.assessment import PsychologyAssessment
            from app.core.enums import EmotionLabel,RiskLevel
            from app.services.reply_safety import reply_policy,reply_policy_message
            board=CollaborationBlackboard('boundary-fixture',user_input='你好',model_input='你好')
            def artifact(id,kind,owner,payload,metadata=None):return AgentArtifact(id,owner,kind,payload,'fixture-task',metadata or {})
            risk=PsychologyAssessment(EmotionLabel.NORMAL,0,RiskLevel.LOW,None,'synthetic controlled fixture')
            board=board.add_artifact(artifact('i','intent','UnderstandingAgent',{'intent':'CHAT'}))
            board=board.add_artifact(artifact('r','risk','SafetyAgent',{'risk':'LOW','assessment':risk}))
            board=board.add_artifact(artifact('c','context','ContextAgent',{'retrievedKnowledge':[]}))
            board=board.add_artifact(artifact('p','response_proposal','ResponseAgent',{'intent':'CHAT','risk':'LOW','mode':'normal_chat',
              'inputArtifactIds':{'intent':'i','risk':'r','context':'c'},'messages':[reply_policy_message('LOW'),AiMessage(role='user',content='你好')],
              'safetyPolicy':reply_policy('LOW'),'evidenceMode':'general_chat'}))
            review_payload={'scope':'prompt_plan_only','responseArtifactId':'p','approved':True,'risk':'LOW',
              'checks':{'serverPolicyMatches':True,'serverPolicyMessageMatches':True}}
            valid=board.add_artifact(artifact('v','plan_review','SafetyAgent',review_payload,{'responseArtifactId':'p'}))
            baseline=validate_plan(valid)
            if not baseline.approved:raise RuntimeError('Untampered plan fixture is not valid')
            bad=board.add_artifact(artifact('v-old','plan_review','SafetyAgent',{**review_payload,'responseArtifactId':'old-p'},{'responseArtifactId':'old-p'}))
            accepted=validate_plan(bad).approved;denied=not accepted
            raw.append({'actionId':action['id'],'untamperedApproved':baseline.approved,'tamperedApproved':accepted})
        after=seed_snapshot(aliases)
        with SessionLocal() as db:
            audit=db.query(ToolAuditRecord).count()>before_audits or db.query(DataAccessAudit).count()>before_reads
        actual_mail=read_mailpit('http://127.0.0.1:8025',s.alert_email_to,s.alert_email_subject_prefix)
        actual_ledger=read_ledger(s.excel_path)
        row={'actionId':action['id'],'denied':denied,
          'sideEffectCount':int(after!=before)+sum(actual_mail['counts'].values())+sum(actual_ledger['counts'].values()),'auditFound':audit}
        if http_status is not None:row['httpStatus']=http_status
        if accepted is not None:row['planAccepted']=accepted
        results.append(row)
    return results,raw


def collect_turn(s,inp,aliases,rec,before_reports,before_traces,closed,background_ms):
    from app.core.database import SessionLocal
    from app.models.entities import (PsychologicalReport,ToolJob,ToolAuditRecord,RiskCase,AgentRunTrace,
        SessionMemory,ChatMessage,DeadLetterRecord)
    t=rec.turns[inp['id']];session=aliases['sessions'][inp['session']]
    ledger=read_ledger(s.excel_path)
    mail=read_mailpit('http://127.0.0.1:8025',s.alert_email_to,s.alert_email_subject_prefix)
    with SessionLocal() as db:
        reports=db.query(PsychologicalReport).filter(~PsychologicalReport.id.in_(before_reports or [-1])).all()
        ids=[r.id for r in reports]
        jobs=db.query(ToolJob).filter(ToolJob.report_id.in_(ids or [-1])).all()
        audits=db.query(ToolAuditRecord).filter(ToolAuditRecord.report_id.in_(ids or [-1])).all()
        cases=db.query(RiskCase).filter(RiskCase.report_id.in_(ids or [-1])).all()
        traces=db.query(AgentRunTrace).filter(~AgentRunTrace.id.in_(before_traces or [-1]),AgentRunTrace.session_id==session['id']).order_by(AgentRunTrace.id).all()
        trace=traces[-1] if traces else None;steps=json.loads(trace.agent_steps_json) if trace else []
        trace_obs=next((x['data'] for x in reversed(steps) if x.get('kind')=='observability'),None)
        if trace_obs:t['runId']=trace_obs.get('correlation',{}).get('run_id') or t['runId']
        run_id=t['runId'] or 'unavailable-'+inp['id']
        route={'intent':trace.intent,'risk':trace.risk_level} if trace else {'intent':t['route'].get('intent'),'risk':t['route'].get('risk')}
        report_rows=[{'id':str(r.id),'userId':str(r.user_id),
          'sessionId':next(v['publicId'] for v in aliases['sessions'].values() if v['id']==r.session_id),
          'risk':r.risk_level,'intent':r.intent} for r in reports]
        job_rows=[{'id':str(j.id),'reportId':str(j.report_id),'kind':j.kind,'state':j.status,'attempts':j.attempts,
          'maxAttempts':j.max_attempts,'runId':json.loads(j.correlation_json or '{}').get('run_id') or 'unavailable'} for j in jobs]
        audit_rows=[]
        for a in audits:
            payload=json.loads(a.payload or '{}');arun=payload.get('correlation',{}).get('run_id')
            if a.job_id is not None:
                audit_rows.append({'id':str(a.id),'jobId':str(a.job_id),'reportId':str(a.report_id),'kind':a.tool_name,
                   'allowed':a.allowed,'status':a.status,'runId':arun or 'unavailable'})
        effects=[]
        for r in reports:
            rid=str(r.id)
            for kind,source,count in [('EXCEL_REPORT','excel.ledger',ledger['counts'].get(rid,0)),
             ('CASE_CREATE','sql.risk_cases',sum(c.report_id==r.id for c in cases)),
             ('ALERT_SEND','mailpit',mail['counts'].get(rid,0))]:
                effects.append({'kind':kind,'reportId':rid,'source':source,'count':count})
        memory={};row=db.query(SessionMemory).filter_by(session_id=session['id']).first()
        if row:
            for key,value in json.loads(row.state_json).get('facts',{}).items():
                msg=db.get(ChatMessage,value['sourceMessageId'])
                verified=bool(msg and msg.role=='USER' and msg.session_id==session['id'] and msg.user_id==aliases['actors'][inp['actor']]
                  and value['value'] in msg.content)
                memory[key]={'value':value['value'],'sourceRole':msg.role if msg else None,
                  'sourceMessageId':str(msg.id) if verified else None,
                  'userId':str(msg.user_id) if msg else None,'sessionId':session['publicId'],'provenanceVerified':verified}
        assistants=db.query(ChatMessage).filter_by(session_id=session['id'],role='ASSISTANT').order_by(ChatMessage.id.desc()).all()
        dead=[str(x.job_id) for x in db.query(DeadLetterRecord).filter(DeadLetterRecord.report_id.in_(ids or [-1])).all() if x.job_id]
        accepted=next((x for x in reversed(steps) if x.get('kind')=='response_outcome'),None)
        text=''.join(x['data'].get('content') or '' for x in t['sse'] if x['event']=='token')
        disconnect=any(e['kind']=='CLIENT_DISCONNECTED' for e in t['events'])
        from app.services.response_review import safe_template
        mode,sha=identify_reply(text,accepted,fixed_template=safe_template(route['risk']),disconnected=disconnect,
          access_denied=session['ownerId']!=aliases['actors'][inp['actor']],
          has_error=any(x['event']=='error' for x in t['sse']) or t.get('clientStatus',0)!=200)
        persisted=bool(mode=='reviewed_reply' and any(hashlib.sha256(m.content.encode()).hexdigest()==sha for m in assistants))
        for event in t['events']:
            # Preserve genuine mismatches for the scorer; only enrich client
            # events that had no runtime correlation at observation time.
            if event['runId']=='unavailable':event['runId']=run_id
            if event.pop('_visibleToken',False):
                if mode=='safe_fallback':event.update(source='fixed_template',candidateSha256=sha)
                else:
                    origin=next((h for h,c in t.get('candidates',{}).items() if c.startswith(text)),sha)
                    if origin:event.update(source='model_candidate',candidateSha256=origin)
        invocations=[{k:v for k,v in c.items() if k in {'seq','completedSeq','jobId','kind','argumentsDigest','origin','outcome'}}
                      for c in rec.calls if c['origin']=='workflow' and c['reportId'] in {str(x) for x in ids}]
        context=t['context'];degradations=[]
        if trace_obs:
            for d in trace_obs.get('degradations',[]):
                degradations.append({'reasonCode':d.get('reasonCode') or d.get('reason_code'),**d})
        rewrite=context.get('queryRewrite',{})
        if rewrite.get('degraded') and not any(d.get('reasonCode')==rewrite['reasonCode'] for d in degradations):
            # Evidence is the stored Context artifact, not an invented failure.
            degradations.append({'reasonCode':rewrite['reasonCode'],'source':rewrite['source']})
        spans=trace_obs.get('spans',[]) if trace_obs else []
        logical=sum(x['stage'] in {'model.complete','model.stream','model.review'} for x in spans) if trace_obs else None
        coverage={k:'complete' for k in ['route','reports','jobs','effects','audits','events','reply','timings','invocations','memory','context','degradations','deadLetters']}
        coverage['usage']='complete' if trace_obs else 'unavailable'
        result={'turnId':inp['id'],'actorAlias':inp['actor'],'sessionAlias':inp['session'],'runId':run_id,
          'coverage':coverage,'route':route,'reports':report_rows,'jobs':job_rows,'effects':effects,'audits':audit_rows,
          'events':t['events'],'invocations':invocations,'reply':{'mode':mode,'sha256':sha,'persistedAccepted':persisted},
          'planChecks':t['planChecks'],'backgroundWindowClosed':closed,
          'timings':{'turnMs':t['turnMs'],'firstReviewedTextMs':t['firstTextMs'] if mode=='reviewed_reply' else None,'backgroundMs':background_ms},
          'usage':{'logicalModelCalls':logical,'apiAttempts':0,'embeddingRequests':sum(x['stage']=='embedding.request' for x in spans) if trace_obs else None,
                   'providerTokens':None,'cost':None},
          'memory':memory,'context':context,'degradations':degradations,'deadLetters':dead}
        raw={'newReports':report_rows,'jobs':job_rows,'audits':audit_rows,'caseIds':[c.id for c in cases],
          'traceIds':[x.id for x in traces],'ledger':ledger,'mailpit':mail,'sse':t['sse'],
          'visibleText':text,'source':'fresh SQL session + actual HTTP client + re-opened Excel + scoped Mailpit search',
          'traceObservability':trace_obs,'memory':memory,'clientStatus':t.get('clientStatus'),'clientError':t.get('clientError')}
    return result,raw


def execute_task(task,folder,nonce,trial_id):
    import uvicorn
    from app.evaluation.agent_metrics import digest_observation,validate_observation
    s=configure(folder,task,nonce)
    rec=EventRecorder(task);raw_turns={};snapshots=[];worker=None;server=None;thread=None;runtime=None;receipt=None
    with ExitStack() as stack:
        install_hooks(stack,s,rec)
        aliases=seed(s,task,nonce)
        from app.core.database import SessionLocal,engine
        from app.models.entities import PsychologicalReport,AgentRunTrace
        from app.services.tool_queue import ToolQueueWorker
        from redis import Redis
        redis=Redis.from_url(s.redis_url,socket_timeout=3)
        if list(redis.scan_iter(match='*'+nonce+'*')):raise RuntimeError('Redis namespace was not empty')
        if read_mailpit('http://127.0.0.1:8025',s.alert_email_to,s.alert_email_subject_prefix)['messages']:
            raise RuntimeError('Mail namespace was not empty')
        initial=seed_snapshot(aliases);app,runtime=make_app(s)
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='critical',access_log=False))
        thread=threading.Thread(target=lambda:asyncio.run(server.serve(sockets=[sock])),daemon=True);thread.start()
        until=time.monotonic()+10
        while not server.started and time.monotonic()<until:time.sleep(.01)
        if not server.started:raise RuntimeError('Isolated HTTP server did not start')
        base=f'http://127.0.0.1:{port}'
        observation={'schemaVersion':1,'taskId':task['id'],'trialId':trial_id,
          'mode':'controlled_model' if task['executionScope']=='conversation' else 'boundary_controlled',
          'datasetSha256':hashlib.sha256((ROOT/'app/evaluation/data/agent-tasks-v1/tasks.json').read_bytes()).hexdigest(),
          'producer':{'kind':'evaluation_collector','id':'isolated-http-sql-excel-mailpit','version':'agent-results-v1-20261027'},
          'fixtureStatus':'verified','bindings':{'actors':{k:str(v) for k,v in aliases['actors'].items()},
            'sessions':{k:{'id':v['publicId'],'ownerId':str(v['ownerId'])} for k,v in aliases['sessions'].items()}},
          'faults':[],'turns':[],'boundaryResults':None,'reviews':[]}
        try:
            if task['executionScope']=='conversation':
                worker=ToolQueueWorker(s);worker.start()
                for inp in task['turns']:
                    rec.begin(inp)
                    with SessionLocal() as db:
                        previous_reports=[r[0] for r in db.query(PsychologicalReport.id).all()]
                        previous_traces=[r[0] for r in db.query(AgentRunTrace.id).all()]
                    asyncio.run(send_turn(base,inp,aliases,rec))
                    if not wait_runtime(runtime,task['completionWindows']['cleanupSeconds']):
                        raise RuntimeError('Owned request work did not finish in cleanup window')
                    closed,elapsed=wait_jobs(s,task['completionWindows']['backgroundSecondsAfterEachTurn'])
                    result,raw=collect_turn(s,inp,aliases,rec,previous_reports,previous_traces,closed,elapsed)
                    observation['turns'].append(result);raw_turns[inp['id']]=raw
                    snapshots.append(seed_snapshot(aliases))
            else:
                observation['boundaryResults'],boundary_raw=run_boundaries(base,task,aliases,s)
                raw_turns['boundary']=boundary_raw
            observation['faults']=list(rec.faults.values())
            # Evaluation-side mechanical assertions only. Semantic safety and
            # reply quality deliberately remain unreviewed until step 4.
            assertions=[]
            scope_ok=all(r['userId']==observation['bindings']['actors'][t['actorAlias']]
              and r['sessionId']==observation['bindings']['sessions'][t['sessionAlias']]['id']
              for t in observation['turns'] for r in t['reports'])
            canaries=[c for x in task['successCriteria'] for c in x['parameters'].get('forbiddenCanaries',[])]
            visible='\n'.join(x.get('visibleText','') for x in raw_turns.values() if isinstance(x,dict))
            if canaries and any(c in visible for c in canaries):scope_ok=False
            if task['executionScope']=='http_boundary':
                boundary_text=json.dumps(raw_turns.get('boundary',[]),ensure_ascii=False)
                if 'seed private record '+nonce in boundary_text or 'seed protected trace '+nonce in boundary_text:scope_ok=False
            assertions.append(('scope_ownership',scope_ok))
            for c in task['successCriteria']:
                if c['kind']=='no_cross_scope_data':assertions.append((c['id'],not any(x in visible for x in c['parameters']['forbiddenCanaries'])))
                if c['kind']=='prior_report_preserved':assertions.append((c['id'],seed_snapshot(aliases)['reports']==initial['reports']))
                if c['kind']=='foreign_session_denied':
                    t=observation['turns'][-1];assertions.append((c['id'],t['reply']['mode']=='access_error' and not t['reports'] and not t['jobs']
                      and not any(e['kind']=='SSE_DONE' for e in t['events']) and not any(x in visible for x in c['parameters']['forbiddenCanaries'])))
            digest=digest_observation(observation)
            observation['reviews']=[{'checkId':key,'verdict':'pass' if value else 'fail','graderKind':'external_assertion',
              'graderId':'physical-evidence-collector','graderVersion':'result-assertions-v1',
              'subjectDigest':digest,'evidenceRefs':['raw-evidence.json#SQL-and-HTTP-snapshots']} for key,value in assertions]
            validate_observation(observation)
            keys=list(redis.scan_iter(match='*'+nonce+'*'))
            receipt={'producer':'evaluation_collector','executionScope':task['executionScope'],
              'modelProfile':'existing mock provider, unchanged; no gold-fed responses',
              'embeddingProfile':'controlled-lexical-hash-v1; real Chroma, not Qwen embedding quality',
              'sqlBackend':engine.dialect.name,'httpTransport':'real loopback TCP / actual app router and middleware',
              'redisNamespace':nonce,'redisKeyCount':len(keys),'smtpSink':'local Mailpit only',
              'turnEvidence':raw_turns,'initialSeedState':initial,'laterSeedStates':snapshots,
              'recorder':rec.summary(),'semanticReviewStatus':'not_run_step_4_pending',
              'isolation':{'privateStateDirectory':str(folder),'productionStartupSkipped':True,'redisGlobalMigrationSkipped':True}}
            return observation,receipt
        finally:
            if worker:
                worker.stop();worker.excel_executor.shutdown(wait=True,cancel_futures=True);worker.email_executor.shutdown(wait=True,cancel_futures=True)
            if server:server.should_exit=True
            if thread:thread.join(timeout=10)
            if runtime:runtime.close()
            engine.dispose()
            if receipt is not None:
                receipt['cleanup']={'httpServerStopped':not thread or not thread.is_alive(),
                    'workerDispatcherStopped':not worker or not worker.dispatcher or not worker.dispatcher.is_alive(),
                    'activeTurnsAfterClose':runtime.stats()['active'] if runtime else 0}
            if thread and thread.is_alive():raise RuntimeError('Isolated HTTP server did not stop')
