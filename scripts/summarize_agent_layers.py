"""Inventory distinct evidence layers without pooling unlike success rates."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def summarize(run,ragas):
    run=run.resolve();ragas=ragas.resolve()
    execution=json.loads((run/'execution-manifest.json').read_text())
    layers={};hashes={};model_names=set();api_attempts=0
    for label in ['controlled','real-nominal','real-context-intervention']:
        folder=run/label;input_file=folder/'observations.json'
        payload=json.loads(input_file.read_text());report_file=folder/('model-reviewed-report.json' if label!='controlled' else 'mechanical-report.json')
        report=json.loads(report_file.read_text());hashes[str(report_file.relative_to(ROOT))]=hashlib.sha256(report_file.read_bytes()).hexdigest()
        layers[label]={'plannedTrials':len(payload['runPlan']),'observations':len(payload['observations']),
          'uniqueTasks':len({x['taskId'] for x in payload['runPlan']}),'invalidObservations':report['invalidObservations'],
          'contractStates':dict(Counter(x['contractStatus'] for x in report['trials'])),
          'cohorts':[{k:v for k,v in c.items() if k in {'split','executionScope','mode','source','plannedTrials','uniqueTasks','rates'}} for c in report['cohorts']]}
    for case in execution['cases']:
        if not case['captured']:continue
        state=ROOT/case['state'];raw=json.loads((state/'raw-evidence.json').read_text())
        for name in ['observation.json','raw-evidence.json']:
            p=state/name;hashes[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
        if case['mode']=='configured_model':
            api_attempts+=raw.get('modelApiRequests',0)
            model_names.update(x['model'] for x in raw.get('providerUsage',[]) if x.get('model'))
    quality=json.loads((ragas/'ragas-report.json').read_text())
    form=json.loads((run/'human-review/form.json').read_text())
    human_file=run/'human-review/agreement.json'
    human=json.loads(human_file.read_text()) if human_file.exists() else {'status':'pending_human_input','plannedSamples':10,'submittedSamples':0,
          'modelAgreement':None,'modelDidNotFillHumanRatings':True}
    repeats=[];real=json.loads((run/'real-nominal/model-reviewed-report.json').read_text())
    for task_id in ['normal-04','normal-08','high-risk-01','high-risk-04','memory-02','evidence-01']:
        trials=sorted([x for x in real['trials'] if x['taskId']==task_id],key=lambda x:x['trialId'])
        repeats.append({'taskId':task_id,'trials':[(x['trialId'],x['contractStatus']) for x in trials]})
    summary={'completedAtUtc':datetime.now(timezone.utc).isoformat(),'stage':4,
      'plannedAgentTrials':75,'uniqueAgentScenarios':len({x['taskId'] for x in execution['plannedTrials']}),
      'evidenceLayers':layers,'requestedGenerationModel':'deepseek-v4-flash','servedGenerationModels':sorted(model_names),
      'actualConfiguredModelApiAttempts':api_attempts,'independentJudgeSameProviderBias':True,
      'ragas':{'library':quality['library'],'version':quality['version'],'subsetSize':12,'summaries':quality['summaries']},
      'humanReview':human,'repeatObservations':repeats,
      'noCrossLayerCompositeScore':True,'noCurrencyCostInvented':True,'rawEvidenceHashes':hashes}
    (run/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (run/'human-review/status.json').write_text(json.dumps(human,ensure_ascii=False,indent=2)+'\n')
    text=['# 第四步证据分层汇总','',
      '各层独立报告，不合并成功率。独立模型评审与生成使用同一供应商；人工未提交前不宣称已人工校准。','',
      '| 证据层 | 试次 | 不同场景 | 契约状态 |','| --- | ---: | ---: | --- |']
    for label,d in layers.items():text.append(f'| {label} | {d["plannedTrials"]} | {d["uniqueTasks"]} | {d["contractStates"]} |')
    text+=['','真实自然任务按划分：','', '| 划分 | 试次 | 契约通过 | 失败/未知 |','| --- | ---: | ---: | --- |']
    for c in real['cohorts']:
        m=c['rates']['contract_pass'];text.append(f'| {c["split"]} | {c["plannedTrials"]} | {m["numerator"]} | unknown={m["unknown"]}；其余未通过 |')
    text+=['','## RAGAS专项（12题，独立RAG子链路）','', '| 指标 | 全部12题均值 | 覆盖率 |','| --- | ---: | ---: |']
    for key,value in quality['summaries'].items():text.append(f'| {key} | {value["fullSetMean"]} | {value["scored"]}/12 |')
    text+=['','人工抽检状态：'+('已收到人工提交' if human_file.exists() else '等待用户回填10条；没有自动代填。'),'',
       '详见 docs/agent-layered-evaluation-v1.md；本页数字不是临床有效性、真实用户准确率或多模型对标结果。']
    (run/'SUMMARY.md').write_text('\n'.join(text)+'\n')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--ragas',type=Path,required=True)
    a=p.parse_args();s=summarize(a.run,a.ragas)
    print('Separate reports saved; human submitted:',s['humanReview']['submittedSamples'])
