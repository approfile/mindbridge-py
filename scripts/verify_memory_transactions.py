"""Real MySQL concurrency regression; isolated DB only, no model/network inference.

--reference-source can load the trusted pre-fix module saved under target/ to
compare the same schedule against the committed baseline without changing it.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reference-source',type=Path)
    parser.add_argument('--batches',type=int,default=5)
    args=parser.parse_args()
    from app.core.config import Settings,get_settings
    from sqlalchemy.engine import make_url
    import re
    original=Settings(); url=make_url(original.database_url)
    if not re.fullmatch(r'mindbridge_bench_[A-Za-z0-9_]+',args.database) or args.database==url.database:
        raise ValueError('Explicit separate benchmark database required')
    folder=args.output.resolve()
    if not folder.is_relative_to(ROOT/'target') or folder.exists(): raise ValueError('Use a new target output directory')
    folder.mkdir(parents=True)
    os.environ.update(DATABASE_URL=url.set(database=args.database).render_as_string(hide_password=False),
                      PRIVACY_KEY_FILE=str(folder/'privacy.key'),AI_PROVIDER='mock',KNOWLEDGE_VECTOR_ENABLED='false',
                      REDIS_URL='redis://redis:6379/14')
    get_settings.cache_clear()
    from app.core.database import Base,engine,SessionLocal
    from app.models.entities import UserAccount,ChatSession,SessionMemory,ChatMessage
    from app.services.session_memory import SessionMemoryService
    from app.services.memory import RedisShortTermMemoryStore
    RedisShortTermMemoryStore._key=lambda self,public_id:'mb-memory-probe:'+folder.name+':'+public_id
    from sqlalchemy import event,inspect,text
    if inspect(engine).get_table_names(): raise ValueError('Fresh empty database required')
    Base.metadata.create_all(engine)
    service_type=SessionMemoryService
    source=ROOT/'app/services/session_memory.py'
    if args.reference_source:
        source=args.reference_source.resolve()
        if not source.is_relative_to(ROOT/'target'): raise ValueError('Reference must be the trusted local target snapshot')
        spec=importlib.util.spec_from_file_location('baseline_session_memory',source)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        service_type=module.SessionMemoryService
    settings=get_settings()
    from app.core.privacy_crypto import cipher
    cipher(settings)
    with SessionLocal() as db:
        user=UserAccount(username='probe',display_name='Synthetic',password_hash='unused')
        db.add(user);db.commit();uid=user.id
    local=threading.local(); gate=[None]
    def synchronize_reads(conn,cursor,statement,parameters,context,executemany):
        if gate[0] and not getattr(local,'synchronized',False) and statement.lstrip().upper().startswith('SELECT') and 'FROM session_memories' in statement:
            local.synchronized=True
            gate[0].wait(timeout=10)
    event.listen(engine,'after_cursor_execute',synchronize_reads)
    results=[]
    def worker(session_id):
        local.synchronized=False
        try:
            with SessionLocal() as db:
                user=db.get(UserAccount,uid);session=db.get(ChatSession,session_id)
                service_type(db,settings,store=object()).synchronize(user,session)
                db.commit()
            return {'sessionId':session_id,'status':'committed','errno':None}
        except Exception as error:
            values=getattr(getattr(error,'orig',None),'args',())
            return {'sessionId':session_id,'status':'failed','errorType':type(error).__name__,
                    'errno':values[0] if values and type(values[0]) is int else None}
    for batch in range(args.batches):
        with SessionLocal() as db:
            sessions=[ChatSession(user_id=uid,public_id=uuid.uuid4().hex,title='Synthetic') for _ in range(4)]
            db.add_all(sessions);db.commit();ids=[row.id for row in sessions]
        gate[0]=threading.Barrier(4)
        with ThreadPoolExecutor(4) as pool: results.extend(pool.map(worker,ids))
    gate[0]=None
    # Serialize multiple writers for ONE session through the existing parent
    # row lock and verify no history/cursor is lost; this checks the other side
    # of the fix, rather than just making unrelated sessions race faster.
    with SessionLocal() as db:
        session=ChatSession(user_id=uid,public_id=uuid.uuid4().hex,title='Same-session probe')
        db.add(session);db.commit();same_id=session.id
    def append(index):
        try:
            from app.agents.harness import MindBridgeAgentHarness
            from app.core.enums import MessageRole
            with SessionLocal() as db:
                user=db.get(UserAccount,uid);session=db.get(ChatSession,same_id)
                MindBridgeAgentHarness(db,settings).save_message(user,session,MessageRole.USER,f'独立合成记录{index}')
            return True
        except Exception: return False
    with ThreadPoolExecutor(4) as pool: appended=list(pool.map(append,range(20)))
    with SessionLocal() as db:
        messages=db.query(ChatMessage).filter_by(session_id=same_id).all()
        memory=db.query(SessionMemory).filter_by(session_id=same_id).one_or_none()
        same_ok=all(appended) and len(messages)==20 and memory is not None and memory.last_message_id==max(r.id for r in messages)
        isolation=db.execute(text('SELECT @@transaction_isolation')).scalar_one()
    # A consistent snapshot can predate the parent lock. The locked child and
    # message reads must still see a writer that committed in the meantime.
    with SessionLocal() as old:
        user=old.get(UserAccount,uid);session=old.get(ChatSession,same_id)
        old.query(SessionMemory).filter_by(session_id=same_id).one()
        with SessionLocal() as newer:
            from app.agents.harness import MindBridgeAgentHarness
            from app.core.enums import MessageRole
            MindBridgeAgentHarness(newer,settings).save_message(newer.get(UserAccount,uid),newer.get(ChatSession,same_id),MessageRole.USER,'项目代号是锁序探针。')
        latest=service_type(old,settings,store=object()).synchronize(user,session)
        fresh=json.loads(latest.state_json).get('facts',{}).get('项目代号',{}).get('value')=='锁序探针'
        old.commit()
    with SessionLocal() as db:
        rollback_session=ChatSession(user_id=uid,public_id=uuid.uuid4().hex,title='Rollback probe')
        db.add(rollback_session);db.commit();rollback_id=rollback_session.id
        service_type(db,settings,store=object()).synchronize(db.get(UserAccount,uid),rollback_session)
        db.rollback()
    with SessionLocal() as db:
        rollback_ok=db.query(SessionMemory).filter_by(session_id=rollback_id).count()==0
    report={'sourceSha256':hashlib.sha256(source.read_bytes()).hexdigest(),'reference':bool(args.reference_source),
            'isolation':isolation,'independentSessions':results,
            'committed':sum(r['status']=='committed' for r in results),
            'deadlocks':sum(r['errno']==1213 for r in results),
            'sameSessionAllAppendsAndCursorCorrect':same_ok,'sameSessionRows':len(messages),
            'freshStateAfterEarlierSnapshot':fresh,'initializationRollsBackWithTransaction':rollback_ok}
    (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='independentSessions'}))
    if not args.reference_source and (report['committed']!=len(results) or not same_ok or not fresh or not rollback_ok): return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
