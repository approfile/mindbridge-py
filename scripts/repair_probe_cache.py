"""Recover only two positively identified synthetic probe caches; preview first.

The early memory probe's same-session append inherited Redis db0 before its
namespace was corrected. Each target must belong to a named probe database,
not exist as a business session, decrypt with its own probe key, and contain
only the exact synthetic append fixture. No broad scan, flush or business read.
"""
import argparse
import base64
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    from app.core.config import Settings
    from sqlalchemy import create_engine,text
    from sqlalchemy.engine import make_url
    from cryptography.fernet import Fernet
    import redis
    settings=Settings();url=make_url(settings.database_url)
    cache=redis.Redis.from_url(settings.redis_url)
    if cache.connection_pool.connection_kwargs.get('db',0)!=0:raise ValueError('This repair is only for the identified db0 probe leak')
    targets=[('mindbridge_bench_memory_before2_20261101','target/memory-transactions/before2-20261101/privacy.key'),
             ('mindbridge_bench_memory_after_20261101','target/memory-transactions/after-20261101/privacy.key')]
    main_engine=create_engine(url,hide_parameters=True)
    proposed=[]
    for database,key_file in targets:
        probe=create_engine(url.set(database=database),hide_parameters=True)
        with probe.connect() as db:
            ids=db.execute(text("SELECT c.public_id FROM chat_sessions c JOIN user_accounts u ON c.user_id=u.id WHERE u.username='probe' AND c.id=21")).scalars().all()
        probe.dispose()
        if len(ids)!=1 or not re.fullmatch(r'[a-f0-9]{32}',ids[0]):raise ValueError('Probe identity mismatch')
        public_id=ids[0]
        with main_engine.connect() as db:
            if db.execute(text('SELECT COUNT(*) FROM chat_sessions WHERE public_id=:id'),{'id':public_id}).scalar_one():raise ValueError('Refuse a business session')
        key='mindbridge:short-term-memory:'+public_id
        cipher=Fernet((ROOT/key_file).read_bytes())
        if cache.type(key)!=b'list':raise ValueError('Expected identified test list')
        values=cache.lrange(key,0,-1)
        if len(values)!=20:raise ValueError('Unexpected probe history length')
        for item in values:
            row=json.loads(item);encrypted=row['content']
            if row.get('role')!='user' or not encrypted.startswith('mbenc:v1:'):raise ValueError('Unexpected row shape')
            plain=cipher.decrypt(encrypted.removeprefix('mbenc:v1:').encode()).decode()
            if not re.fullmatch(r'独立合成记录(?:[0-9]|1[0-9])',plain):raise ValueError('Not the synthetic probe fixture')
        proposed.extend([key,key+':version'])
    main_engine.dispose()
    evidence={'keys':proposed,'verifiedProbeLists':2,'verifiedFixtureMessages':40,'businessSessionMatches':0,'applied':False}
    destination=ROOT/'target/probe-cache-repair-20261101';destination.mkdir(exist_ok=True)
    if args.apply:
        with cache.pipeline() as pipe:
            pipe.watch(*proposed)
            backup=[{'key':key,'dump':base64.b64encode(pipe.dump(key)).decode(),'pttl':pipe.pttl(key)} for key in proposed]
            (destination/'encrypted-backup.json').write_text(json.dumps(backup,indent=2)+'\n',encoding='utf-8')
            pipe.multi();pipe.delete(*proposed);removed=pipe.execute()[0]
        if removed!=4:raise RuntimeError('Unexpected removed key count')
        evidence.update(applied=True,removedKeys=removed)
    (destination/('applied.json' if args.apply else 'preview.json')).write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(evidence))


if __name__=='__main__':main()
