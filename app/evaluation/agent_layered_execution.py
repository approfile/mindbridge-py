"""Evaluation-only configured-model adapter over the step-3 isolated collector."""
from __future__ import annotations

from contextlib import ExitStack,asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]


def execute_configured(task,folder,nonce,trial_id):
    # Read configured credentials BEFORE the isolated adapter exports mock env.
    from app.core.config import Settings,get_settings
    configured=Settings()
    from app.services.agent_models import AgentModelRegistry
    profiles={role:vars(AgentModelRegistry(configured).profile_for(role)) for role in
              ['UnderstandingAgent','SafetyAgent','ContextAgent','ResponseAgent']}
    if any(p['provider']!='openai' for p in profiles.values()):raise ValueError('Configured v1 adapter requires OpenAI-compatible profiles')
    from app.evaluation import agent_execution as core
    original_configure=core.configure;original_hooks=core.install_hooks;original_collect=core.collect_turn
    allowed={'ai_provider','ai_temperature','ai_max_tokens','openai_api_key','openai_base_url','openai_model',
      'openai_thinking_mode','openai_embedding_model','openai_embedding_api_key','openai_embedding_base_url',
      'embedding_timeout_seconds','reply_review_json_mode','ollama_base_url','ollama_model'}
    usage=[];attempts=[];active_recorder=[None]
    def configure(*args):
        s=original_configure(*args)
        for key,value in configured.model_dump().items():
            if key in allowed or key.startswith('agent_model_'):setattr(s,key,value)
        for key,value in s.model_dump().items():os.environ[key.upper()]=str(value).lower() if isinstance(value,bool) else str(value)
        get_settings.cache_clear();return s
    def hooks(stack,s,rec):
        import httpx
        from app.services.vector_store import ChromaKnowledgeStore
        from app.agents.events import CollaborationBlackboard
        original_embed=ChromaKnowledgeStore._embed
        original_hooks(stack,s,rec);active_recorder[0]=rec
        cache_path=ROOT/'.local-runtime/chunk-size-ab-20261025/embedding-cache.json'
        provenance=json.loads((ROOT/'target/chunk-size-ab/report.json').read_text())
        cache=json.loads(cache_path.read_text()) if s.openai_embedding_model==provenance['model'] else {}
        def embed(store,texts):
            rec.embedding_calls+=1
            if getattr(rec.thread,'query',False):
                if rec.has('raise_timeout','embedding.request'):
                    rec.trigger('raise_timeout','embedding.request');raise httpx.ReadTimeout('controlled embedding outage')
                return original_embed(store,texts)
            keys=[hashlib.sha256(t.encode()).hexdigest() for t in texts]
            if all(k in cache for k in keys):return [cache[k] for k in keys]
            return original_embed(store,texts)
        stack.enter_context(patch.object(ChromaKnowledgeStore,'_embed',embed))
        add=CollaborationBlackboard.add_artifact
        def capture(board,artifact):
            result=add(board,artifact)
            if rec.active:
                turn=rec.turns[rec.active]
                if artifact.kind=='context':
                    turn['actualRetrievedContexts']=[{'source':x.source,'content':x.content} for x in artifact.payload.get('retrievedKnowledge',[])]
                if artifact.kind=='response_proposal':turn['actualResponseMessages']=[m.model_dump() for m in artifact.payload.get('messages',[])]
            return result
        stack.enter_context(patch.object(CollaborationBlackboard,'add_artifact',capture))
        host=__import__('urllib.parse',fromlist=['urlparse']).urlparse(s.openai_base_url).hostname
        send=httpx.Client.send;asend=httpx.AsyncClient.send;lines=httpx.Response.aiter_lines;astream=httpx.AsyncClient.stream
        def record(data,kind,status):
            if rec.active:usage.append({'turnId':rec.active,'kind':kind,'status':status,'requestId':data.get('id'),
                'model':data.get('model'),'usage':data.get('usage')})
        def sync_send(client,request,*args,**kwargs):
            if request.url.host==host and request.url.path.endswith('/chat/completions'):
                attempts.append({'turnId':rec.active,'kind':'sync'})
            response=send(client,request,*args,**kwargs)
            if request.url.host==host and request.url.path.endswith('/chat/completions'):
                if not kwargs.get('stream',False):record(response.json() if response.is_success else {},'sync',response.status_code)
            return response
        async def async_send(client,request,*args,**kwargs):
            if request.url.host==host and request.url.path.endswith('/chat/completions'):
                attempts.append({'turnId':rec.active,'kind':'stream' if kwargs.get('stream',False) else 'review'})
            response=await asend(client,request,*args,**kwargs)
            if request.url.host==host and request.url.path.endswith('/chat/completions') and not kwargs.get('stream',False):
                record(response.json() if response.is_success else {},'review',response.status_code)
            return response
        @asynccontextmanager
        async def stream(client,method,url,**kwargs):
            payload=kwargs.get('json')
            if str(url).startswith(s.openai_base_url) and isinstance(payload,dict) and payload.get('stream'):
                kwargs['json']={**payload,'stream_options':{'include_usage':True}}
            async with astream(client,method,url,**kwargs) as response:yield response
        async def capture_lines(response):
            async for line in lines(response):
                if response.request.url.host==host and line.startswith('data: ') and line[6:].strip()!='[DONE]':
                    try:
                        d=json.loads(line[6:])
                        if d.get('usage'):record(d,'stream',response.status_code)
                    except ValueError:pass
                yield line
        stack.enter_context(patch.object(httpx.Client,'send',sync_send));stack.enter_context(patch.object(httpx.AsyncClient,'send',async_send))
        stack.enter_context(patch.object(httpx.AsyncClient,'stream',stream));stack.enter_context(patch.object(httpx.Response,'aiter_lines',capture_lines))
    def collect(s,inp,aliases,rec,*args):
        result,raw=original_collect(s,inp,aliases,rec,*args)
        t=rec.turns[inp['id']]
        raw['actualRetrievedContexts']=t.get('actualRetrievedContexts',[])
        raw['actualResponseMessages']=t.get('actualResponseMessages',[])
        entries=[x for x in usage if x['turnId']==inp['id']]
        unique={x['requestId']:x for x in entries if x['requestId'] and x['usage']}
        total={k:sum(x['usage'].get(k,0) for x in unique.values()) for k in ['prompt_tokens','completion_tokens','total_tokens']}
        logical=result['usage']['logicalModelCalls']
        complete=bool(unique) and logical is not None and len(unique)==logical
        result['usage']['apiAttempts']=sum(x['turnId']==inp['id'] for x in attempts)
        result['usage']['providerTokens']={'source':'provider_usage','complete':complete,'prompt':total['prompt_tokens'],
                'completion':total['completion_tokens'],'total':total['total_tokens']} if unique else None
        raw['providerUsage']=entries;return result,raw
    with ExitStack() as stack:
        stack.enter_context(patch.object(core,'configure',configure));stack.enter_context(patch.object(core,'install_hooks',hooks))
        stack.enter_context(patch.object(core,'collect_turn',collect))
        observation,raw=core.execute_task(task,folder,nonce,trial_id)
    observation['mode']='configured_model'
    from app.evaluation.agent_metrics import digest_observation,validate_observation
    digest=digest_observation(observation)
    for r in observation['reviews']:
        if r['graderKind']!='external_assertion' or r['graderId']!='physical-evidence-collector':raise ValueError('Cannot rebind another evaluator')
        r['subjectDigest']=digest
    raw['modelProfile']=profiles;raw['embeddingProfile']=configured.openai_embedding_model
    raw['providerUsage']=usage;raw['apiAttempts']=attempts;raw['modelApiRequests']=len(attempts)
    raw['semanticReviewStatus']='pending_independent_judge'
    validate_observation(observation)
    return observation,raw
