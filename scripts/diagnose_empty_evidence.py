"""Synthetic empty-evidence body pipeline probe. Private candidate/review evidence.

No real conversation is read; the input is the frozen benchmark synthetic case.
"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    from app.core.config import Settings
    from app.services.ai import AiClient,PromptTemplates
    from app.core.enums import IntentType,RiskLevel
    from app.schemas.dtos import AiMessage
    from app.services.response_review import ReplyReviewPipeline
    from app.services.observability import observation_scope
    from app.services.model_gateway import usage_snapshot
    from app.benchmark.protocol import load_dataset
    from datetime import datetime,timezone
    settings=Settings()
    case=next(c for c in load_dataset()[0]['nominal'] if c['id']=='evidence-insufficient')
    folder=ROOT/'target/empty-evidence-diagnostics'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder.mkdir(parents=True)
    reports=[]
    class Recorder(AiClient):
        async def stream(self,messages):
            text=''
            try:
                async for token in super().stream(messages): text+=token;yield token
            finally: captured['candidates'].append(text)
        async def complete_async(self,messages):
            answer=await super().complete_async(messages)
            captured['reviews'].append(answer)
            return answer
    for index in range(3):
        captured={'candidates':[],'reviews':[]}
        generator=Recorder(settings);generator.agent_name='ResponseAgent'
        reviewer=Recorder(settings);reviewer.agent_name='SafetyAgent'
        outcome=SimpleNamespace(plan_accepted=True,budget_exhausted=False,risk_level='LOW',intent=IntentType.CONSULT,
                                model_input=case['turns'][0],retrieved_knowledge=[],memory_facts={},
                                response_messages=[PromptTemplates.answer_system_prompt(IntentType.CONSULT,RiskLevel.LOW,'[]','学生'),
                                                   AiMessage(role='user',content=case['turns'][0])])
        with observation_scope(ensure_run=True,reuse=False) as state:
            reply=asyncio.run(ReplyReviewPipeline(settings,generator,reviewer).run(outcome))
        report={'trial':index+1,'accepted':reply.accepted,'status':reply.status,'events':reply.events,'usage':usage_snapshot(state),**captured}
        reports.append(report)
        print(json.dumps({'trial':index+1,'accepted':reply.accepted,'violations':[e.get('violations') for e in reply.events if e['kind']=='response_review']}),flush=True)
    (folder/'report.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Private evidence: '+str(folder/'report.json'))


if __name__=='__main__':main()
