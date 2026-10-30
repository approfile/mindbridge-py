"""Eight source-instruction attack variants; not eight independent scenarios."""
import argparse,asyncio,hashlib,json
from pathlib import Path

from app.core.config import Settings
from app.evaluation.reply_safety_runner import ROOT,outcome_for
from app.services.agent_models import AgentModelRegistry
from app.services.model_usage import measure_model_calls
from app.services.response_review import ReplyReviewPipeline,deterministic_violations

async def evaluate(provider,output):
    path=ROOT/'app/evaluation/data/reply-safety-source-v1.json';raw=path.read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    manifest=json.loads((ROOT/'app/evaluation/data/frozen-manifest.json').read_text())
    if manifest.get(path.relative_to(ROOT).as_posix())!=digest:raise ValueError('Source boundary data changed')
    data=json.loads(raw);settings=Settings(_env_file=str(ROOT/'.env'),reply_max_revisions=0)
    if provider=='mock':settings.ai_provider=settings.agent_model_default_provider=settings.agent_model_safety_provider='mock'
    registry=AgentModelRegistry(settings)
    if provider=='configured' and registry.profile_for('SafetyAgent').provider not in {'openai','ollama'}:
        raise ValueError('Real model provider required')
    judge=registry.client_for('SafetyAgent');rows=[]
    for case in data['cases']:
        outcome=outcome_for(case)
        class Fixed:
            async def stream(self,messages):yield case['candidate']
        if deterministic_violations(case['candidate'],case['risk'],''):
            raise ValueError('Source variants must reach the model review')
        with measure_model_calls() as usage:reply=await ReplyReviewPipeline(settings,Fixed(),judge).run(outcome)
        rows.append({'id':case['id'],'expectedApproved':case['expectedApproved'],'accepted':reply.accepted,
                     'status':reply.status,'events':reply.events,'usage':usage})
        print(case['id'],reply.accepted,flush=True)
    report={'cases':len(rows),'correct':sum(r['accepted']==r['expectedApproved'] for r in rows),
            'datasetSha256':digest,'provider':provider,'annotation':data['annotation'],'results':rows}
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2));return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--provider',choices=['mock','configured'],default='mock')
    p.add_argument('--output',default='target/reply-safety/source-boundaries.json');a=p.parse_args()
    r=asyncio.run(evaluate(a.provider,a.output));print(json.dumps({k:v for k,v in r.items() if k!='results'},ensure_ascii=False))

if __name__=='__main__':main()
