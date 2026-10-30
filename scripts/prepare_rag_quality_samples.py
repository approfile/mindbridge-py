"""Real RAG component pipeline; references are never passed to generation."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


async def run():
    from app.core.config import Settings,get_settings
    source=Settings();nonce=uuid.uuid4().hex[:12];private=ROOT/'.local-runtime/agent-layered-20261027'/('rag-'+nonce);private.mkdir();os.chmod(private,0o700)
    out=ROOT/'target/agent-layered/ragas-v1';out.mkdir(parents=True,exist_ok=False)
    references=ROOT/'app/evaluation/data/agent-layered-v1/ragas-references.json'
    data=json.loads(references.read_text());manifest=json.loads(references.with_name('ragas-references.manifest.json').read_text())
    assert hashlib.sha256(references.read_bytes()).hexdigest()==manifest['sha256']
    s=source.model_copy(update={'database_url':f'sqlite:///{private/"rag.sqlite3"}',
      'privacy_key_file':str(private/'key'),'chroma_persist_dir':str(private/'chroma'),
      'chroma_snapshot_dir':str(private/'snapshots'),'chroma_snapshot_keep':1,'knowledge_vector_required':True})
    for k,v in s.model_dump().items():os.environ[k.upper()]=str(v).lower() if isinstance(v,bool) else str(v)
    os.environ['ANONYMIZED_TELEMETRY']='false';get_settings.cache_clear()
    from sqlalchemy.orm import Session
    from app.core.database import Base,engine
    from app.core.privacy_crypto import cipher
    from app.services.knowledge import KnowledgeService
    from app.services.vector_store import ChromaKnowledgeStore
    from app.services.agent_models import AgentModelRegistry
    from app.services.ai import PromptTemplates
    from app.schemas.dtos import AiMessage
    from app.core.enums import IntentType,RiskLevel
    from app.services.query_rewrite import rewrite_query
    from app.services.response_review import ReplyReviewPipeline
    from app.services.reply_safety import reply_policy_message
    from app.agents.autonomous import ContextAgent
    cipher(s);Base.metadata.create_all(engine)
    cache=json.loads((ROOT/'.local-runtime/chunk-size-ab-20261025/embedding-cache.json').read_text());original=ChromaKnowledgeStore._embed
    def cached(store,texts):
        keys=[hashlib.sha256(t.encode()).hexdigest() for t in texts]
        return [cache[k] for k in keys] if s.openai_embedding_model=='qwen3.7-text-embedding' and all(k in cache for k in keys) else original(store,texts)
    registry=AgentModelRegistry(s);results=[]
    (out/'plan.json').write_text(json.dumps({'referenceSha256':manifest['sha256'],'plannedIds':[x['id'] for x in data['samples']],
      'scope':'isolated RAG component: rewrite + actual hybrid retrieval/neighbor expansion + response + runtime review; not full Agent routing',
      'privateState':str(private),'generationModel':registry.profile_for('ResponseAgent').model,'embeddingModel':s.openai_embedding_model},ensure_ascii=False,indent=2)+'\n')
    try:
        with Session(engine) as db:
            service=KnowledgeService(db,s)
            with patch.object(ChromaKnowledgeStore,'_embed',cached):
                for name,digest in data['corpusHashes'].items():
                    p=ROOT/'app/knowledge'/name;assert hashlib.sha256(p.read_bytes()).hexdigest()==digest
                    service.ensure_source(name,p.read_text())
            for sample in data['samples']:
                row={**sample,'status':'pending'}
                try:
                    q=sample['question']
                    rw=rewrite_query(registry.client_for('ContextAgent'),[
                      AiMessage(role='system',content=ContextAgent.profile.system_prompt+'\n把学生输入改写成适合检索校园心理知识库的中文查询词，只输出查询词。'),
                      AiMessage(role='user',content='记忆摘要：\n无相关历史记忆。\n\n当前输入：\n'+q)],q)
                    hits=service.retrieve(rw.query,4,mode='hybrid',expand=True)
                    contexts=[h.content for h in hits];knowledge='\n\n'.join(f'- [{h.source}] {h.content}' for h in hits)
                    messages=[reply_policy_message('LOW'),PromptTemplates.answer_system_prompt(IntentType.CONSULT,RiskLevel.LOW,knowledge,'学生'),AiMessage(role='user',content=q)]
                    outcome=SimpleNamespace(plan_accepted=True,budget_exhausted=False,risk_level='LOW',response_messages=messages,
                        model_input=q,retrieved_knowledge=hits,memory_facts={})
                    reply=await ReplyReviewPipeline(s,registry.client_for('ResponseAgent'),registry.client_for('SafetyAgent')).run(outcome)
                    row.update(status='generated' if reply.accepted else 'safe_fallback',response=reply.text,retrieved_contexts=contexts,
                       sources=[h.source for h in hits],queryRewrite=rw.metadata(),actualQuery=rw.query,
                       runtimeReviewStatus=reply.status,reviewEvents=reply.events)
                except Exception as e:row.update(status='failed',errorType=type(e).__name__)
                results.append(row);(out/'samples.json').write_text(json.dumps({'planSize':12,'samples':results},ensure_ascii=False,indent=2)+'\n')
                print('RAG_SAMPLE',sample['id'],row['status'],flush=True)
    finally:engine.dispose()


if __name__=='__main__':asyncio.run(run())
