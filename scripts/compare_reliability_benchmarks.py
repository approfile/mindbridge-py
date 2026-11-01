"""Export a before/after summary with identical dataset and offline grader."""
import argparse
import hashlib
import json
from pathlib import Path


def load(path):return json.loads(path.read_text(encoding='utf-8'))
def checksum(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(before,regraded,after,memory_before,memory_after,output):
    aplan=load(before/'plan.json');bplan=load(after/'plan.json')
    old=load(before/'report.json');new=load(after/'report.json');grade=load(regraded/'report.json')
    a=load(memory_before);b=load(memory_after)
    if aplan['datasetSha256']!=bplan['datasetSha256']:raise ValueError('Different datasets')
    if grade['sourceObservationsSha256']!=checksum(before/'private/observations.json'):raise ValueError('Regrade is for different observations')
    if grade['graderVersion']!=new['graderVersion']:raise ValueError('Do not compare different graders')
    if aplan['plannedTrials']!=bplan['plannedTrials']:raise ValueError('Different planned trials')
    expected_judgments=sum(row['layer']!='admission_probe' for row in aplan['plannedTrials'])
    if grade.get('calibrationSubset') or grade['evaluations']!=expected_judgments:raise ValueError('Calibration subset is not a full regrade')
    original_judgments=load(before/'private/judgments.json')
    after_judgments=load(after/'private/judgments.json')
    fields=['modelProfiles','embeddingModel','limits','settings','corpusHashes','connections']
    if any(aplan[key]!=bplan[key] for key in fields):raise ValueError('Execution settings differ beyond the intended code changes')
    source='app/services/session_memory.py'
    if a['sourceSha256']!=aplan['sourceHashes'][source] or b['sourceSha256']!=bplan['sourceHashes'][source]:raise ValueError('Transaction probe source mismatch')
    groups={}
    for key,value in grade['groups'].items():
        if key not in new['groups']:continue
        groups[key]={'beforeSameGrader':value,'after':new['groups'][key]['taskContract']}
    case_context=lambda rows:{k:v for k,v in rows.items() if k.endswith('/context_intervention')}
    def diagnostics(report):
        return {key:row['databaseDiagnostics'] for key,row in report['groups'].items() if row['databaseDiagnostics']}
    result={'datasetSha256':aplan['datasetSha256'],'graderVersion':grade['graderVersion'],
            'protocol':'baseline-reliability-comparison-v1','identicalExecutionConfiguration':True,
            'beforeRuntimePolicy':old.get('replyPolicyVersion','reply-safety-v3'),
            'afterRuntimePolicy':new['replyPolicyVersion'],
            'sourceChanges':{key:{'before':aplan['sourceHashes'].get(key),'after':bplan['sourceHashes'].get(key)}
                             for key in sorted(set(aplan['sourceHashes'])|set(bplan['sourceHashes'])) if aplan['sourceHashes'].get(key)!=bplan['sourceHashes'].get(key)},
            'plannedScenarios':new['plannedScenarios'],'capturedScenarios':new['capturedScenarios'],
            'controlledTransactionProbe':{'before':{k:v for k,v in a.items() if k!='independentSessions'},
                                          'after':{k:v for k,v in b.items() if k!='independentSessions'}},
            'databaseDiagnostics':{'before':diagnostics(old),'after':diagnostics(new)},
            'qualitySameGrader':groups,
            'emptyEvidenceSameGrader':case_context(groups),
            'beforeCohortPerformance':old['cohortPerformance'],'afterCohortPerformance':new['cohortPerformance'],
            'afterAdmission':new['admission'],'afterPhysicalEvidence':new['physicalEvidence'],
            'graderEvaluation':{'originalV1':{'evaluations':len(original_judgments),'valid':sum(j['decision'] is not None for j in original_judgments)},
                                'originalObservationsRegraded':{'evaluations':grade['evaluations'],'valid':grade['valid'],'invalidAttempts':grade['invalidAttempts']},
                                'after':{'evaluations':len(after_judgments),'valid':sum(j['decision'] is not None for j in after_judgments)}},
            'regradeCost':grade['offlineJudgeUsage']['cost'],'afterCostScopes':new['costScopes'],
            'artifactHashes':{'beforeObservations':checksum(before/'private/observations.json'),'beforePlan':checksum(before/'plan.json'),
                              'sameGraderRegrade':checksum(regraded/'report.json'),'afterReport':checksum(after/'report.json'),
                              'afterObservations':checksum(after/'private/observations.json'),'afterPlan':checksum(after/'plan.json'),
                              'memoryBefore':checksum(memory_before),'memoryAfter':checksum(memory_after)},
            'limitations':['Synthetic developer-visible benchmark; repeated trials are not independent unseen questions.',
                           'Same-provider model grader, no expert safety certification.',
                           'Shared cloud caches/provider load and different outputs confound cost and latency attribution.',
                           'Native deadlock probe intentionally synchronizes reads; not a production deadlock probability.']}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('COMPARED '+str(output))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['before','regraded','after','memory-before','memory-after','output']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    compare(args.before,args.regraded,args.after,args.memory_before,args.memory_after,args.output)
