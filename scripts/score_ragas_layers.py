"""Run the actual RAGAS 0.4.3 metrics in its separate evaluation environment."""
import argparse
import asyncio
from contextvars import ContextVar
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['RAGAS_DO_NOT_TRACK']='true'
ACTIVE=ContextVar('ragas-eval-sample',default=None)


def summarize_rows(results,name,planned=12):
    rows=[x for x in results if x['metric']==name]
    if len({x['sampleId'] for x in rows})!=len(rows) or len(rows)!=planned:raise ValueError('Metric sample coverage changed or duplicated')
    valid=[x['value'] for x in rows if x['status']=='scored'];partial=[x for x in rows if x['status']=='scored_partial_judge']
    if any(not isinstance(x,(int,float)) or isinstance(x,bool) or not math.isfinite(x) for x in valid):raise ValueError('Non-finite RAGAS value')
    return {'planned':planned,'scored':len(valid),'partial':len(partial),'failedOrSkipped':planned-len(valid)-len(partial),
          'coverage':len(valid)/planned,'meanOnScored':sum(valid)/len(valid) if valid else None,
          'fullSetMean':sum(valid)/planned if len(valid)==planned else None}


async def run(folder,output=None):
    import httpx
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.embeddings.base import embedding_factory
    from ragas.metrics.collections import Faithfulness,AnswerRelevancy,ContextRecall,ContextRelevance
    from app.core.config import Settings
    output=output or folder/'ragas-report.json'
    if output.exists():raise ValueError('Existing RAGAS report is preserved; choose a new --output path')
    s=Settings();calls=[];ratings={};results=[];semaphore=asyncio.Semaphore(2)
    async def response_hook(response):
        await response.aread()
        try:d=response.json()
        except ValueError:d={}
        calls.append({'evaluation':ACTIVE.get(),'status':response.status_code,'model':d.get('model'),
             'requestId':d.get('id'),'usage':d.get('usage')})
    http=httpx.AsyncClient(timeout=60,event_hooks={'response':[response_hook]},trust_env=False)
    client=AsyncOpenAI(api_key=s.openai_api_key,base_url=s.openai_base_url,http_client=http,max_retries=1)
    embedding_client=AsyncOpenAI(api_key=s.openai_embedding_api_key or s.openai_api_key,
          base_url=s.openai_embedding_base_url,http_client=http,max_retries=1)
    llm=llm_factory(s.openai_model,client=client,temperature=0,max_tokens=4096,extra_body={'thinking':{'type':'disabled'}})
    original=llm.agenerate
    async def generate(*args,**kwargs):
        value=await original(*args,**kwargs)
        if hasattr(value,'rating'):ratings.setdefault(ACTIVE.get(),[]).append(value.rating)
        return value
    llm.agenerate=generate
    embedding=embedding_factory('openai',model=s.openai_embedding_model,client=embedding_client,interface='modern')
    metrics={'faithfulness':Faithfulness(llm=llm),'answer_relevancy':AnswerRelevancy(llm=llm,embeddings=embedding,strictness=3),
             'context_recall':ContextRecall(llm=llm),'context_relevance':ContextRelevance(llm=llm,max_retries=2)}
    data=json.loads((folder/'samples.json').read_text());assert len(data['samples'])==12
    header={'library':'ragas','version':importlib.metadata.version('ragas'),'plannedSamples':12,
      'metrics':list(metrics),'judgeRequestedModel':s.openai_model,'embeddingModel':s.openai_embedding_model,
      'sameProviderBias':True,'referenceIndependentReview':'pending','results':results}
    async def score(sample,name,metric):
        key=sample['id']+':'+name;row={'sampleId':sample['id'],'metric':name,'value':None,'status':'pending'}
        if name in {'faithfulness','answer_relevancy'} and sample['status']!='generated':
            row['status']='not_scored_generation_failed_or_fallback';results.append(row);return
        if not sample.get('retrieved_contexts'):
            row['status']='not_scored_context_missing';results.append(row);return
        async with semaphore:
            token=ACTIVE.set(key);started=time.perf_counter()
            try:
                kwargs={'user_input':sample['question']}
                if name in {'faithfulness','answer_relevancy'}:kwargs['response']=sample['response']
                if name!='answer_relevancy':kwargs['retrieved_contexts']=sample['retrieved_contexts']
                if name=='context_recall':kwargs['reference']=sample['reference']
                value=await asyncio.wait_for(metric.ascore(**kwargs),timeout=180)
                numeric=float(value.value)
                if not math.isfinite(numeric):raise ValueError('RAGAS returned non-finite score')
                row.update(status='scored',value=numeric,reason=value.reason)
                if name=='context_relevance':
                    valid=[x for x in ratings.get(key,[]) if x in [0,1,2]]
                    row['validJudgeRatings']=valid
                    if len(valid)<2:row['status']='scored_partial_judge'
            except Exception as e:row.update(status='failed',errorType=type(e).__name__)
            finally:
                row['elapsedMs']=(time.perf_counter()-started)*1000;ACTIVE.reset(token)
                results.append(row);header['results']=results;output.write_text(json.dumps(header,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
                print('RAGAS',key,row['status'],row['value'],flush=True)
    try:
        await asyncio.gather(*(score(x,name,m) for x in data['samples'] for name,m in metrics.items()))
    finally:await http.aclose()
    header['summaries']={}
    for name in metrics:
        header['summaries'][name]=summarize_rows(results,name)
    header['providerCalls']=calls
    output.write_text(json.dumps(header,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--samples',type=Path,required=True);p.add_argument('--output',type=Path)
    a=p.parse_args();asyncio.run(run(a.samples,a.output))
