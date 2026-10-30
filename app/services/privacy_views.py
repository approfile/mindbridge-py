"""Default metadata projection plus sanitized, explicitly authorized details."""
from app.services.privacy import PrivacySanitizer

RESTRICTED='[敏感正文已隐藏]'
ORIGINAL_HIDDEN='[原文不通过接口提供]'
EXPIRED='[已超过正文保留期]'


def sanitized_value(value):
    privacy=PrivacySanitizer()
    if isinstance(value,str):return privacy.sanitize(value)
    if isinstance(value,list):return [sanitized_value(v) for v in value]
    if isinstance(value,dict):return {privacy.sanitize(str(k)):sanitized_value(v) for k,v in value.items()}
    return value


def technical_steps(steps):
    result=[]
    for item in steps:
        kind=item.get('kind')
        if kind in {'observability','run_correlation','turn_control'}:
            result.append(item)  # produced by server-only structured collectors
        elif kind=='agent_event':
            result.append({k:v for k,v in item.items() if k in {'kind','type','actor','taskId','artifactId'}})
        elif kind=='agent_artifact':
            result.append({k:v for k,v in item.items() if k in {'kind','id','owner','artifactKind','taskId'}})
        elif kind=='agent_task':
            result.append({k:v for k,v in item.items() if k in {'kind','id','status','priority','requiredCapabilities','claimedBy','createdBy'}})
        elif kind in {'response_review','response_outcome'}:
            result.append({k:v for k,v in item.items() if k in {'kind','type','status','attempt','attempts','approved','policyVersion','violations','ruleViolations','elapsedMs','generationMs','ruleReviewMs','modelReviewMs','finalSha256','candidateSha256'}})
        elif 'step' in item:
            result.append({k:v for k,v in item.items() if k in {'step','agent','action'}})
    return result
