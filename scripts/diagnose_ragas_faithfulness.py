"""Post-score diagnostics only; never replace the preregistered RAGAS scores."""
import asyncio
import json
import os
from pathlib import Path
import sys
os.environ['RAGAS_DO_NOT_TRACK']='true'
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


async def main():
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import Faithfulness
    from app.core.config import Settings
    s=Settings();folder=ROOT/'target/agent-layered/ragas-v1'
    base=json.loads((folder/'ragas-report.json').read_text());samples=json.loads((folder/'samples.json').read_text())['samples']
    chosen=sorted([r for r in base['results'] if r['metric']=='faithfulness'],key=lambda r:r['value'])[:2]
    client=AsyncOpenAI(api_key=s.openai_api_key,base_url=s.openai_base_url,max_retries=1,timeout=60)
    llm=llm_factory(s.openai_model,client=client,temperature=0,max_tokens=4096,extra_body={'thinking':{'type':'disabled'}})
    original=llm.agenerate;traces=[]
    async def trace(*args,**kwargs):
        value=await original(*args,**kwargs)
        traces.append({'outputType':type(value).__name__,'output':value.model_dump()})
        return value
    llm.agenerate=trace;metric=Faithfulness(llm=llm);rows=[]
    try:
        for entry in chosen:
            sample=next(x for x in samples if x['id']==entry['sampleId']);traces.clear()
            try:
                value=await metric.ascore(user_input=sample['question'],response=sample['response'],retrieved_contexts=sample['retrieved_contexts'])
                rows.append({'sampleId':sample['id'],'originalScore':entry['value'],'diagnosticScore':float(value.value),'structuredTraces':list(traces)})
            except Exception as e:rows.append({'sampleId':sample['id'],'originalScore':entry['value'],'errorType':type(e).__name__})
    finally:await client.close()
    (folder/'faithfulness-diagnostics.json').write_text(json.dumps({'selection':'two lowest original faithfulness scores; post-hoc diagnostics, not new benchmark data',
      'originalResultsUnchanged':True,'rows':rows},ensure_ascii=False,indent=2)+'\n')
    print('Saved diagnostic claim-level evidence; primary scores unchanged.')


if __name__=='__main__':asyncio.run(main())
