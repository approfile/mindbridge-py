"""Verifiable preparation contract. It does not grade the ungenerated reply."""
from dataclasses import asdict, dataclass

from app.agents.events import AgentEventType
from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.reply_safety import reply_policy, reply_policy_message

VERSION = 'plan-contract-v1'
CLARIFICATION_MESSAGE = AiMessage(role='system', content='本轮存在未能确认的安全信号，请先简短确认当前是否安全，并引导现实支持；不要给诊断结论。')


@dataclass(frozen=True)
class PlanValidation:
    approved: bool
    checks: dict
    violations: tuple[str, ...]
    inputArtifactIds: dict
    evidenceMode: str
    policyVersion: str = VERSION
    scope: str = 'prompt_plan_structure_only'
    confidenceUsed: bool = False

    def to_dict(self):
        return asdict(self)


def _payload(artifact):
    return artifact.payload if artifact and isinstance(artifact.payload, dict) else {}


def validate_plan(board):
    response = board.latest_artifact('response_proposal')
    review = board.latest_artifact('plan_review')
    intent_artifact = board.latest_artifact('intent')
    risk_artifact = board.latest_artifact('risk')
    context = board.latest_artifact('context') or board.latest_artifact('memory')
    payload = _payload(response); checked = _payload(review)
    assessed = _payload(risk_artifact).get('assessment')
    risk = intent = None
    try:
        levels = [RiskLevel(_payload(a)['risk']) for a in board.artifacts_by_kind('risk')]
        risk = max(levels, key=lambda level: {'LOW': 1, 'MEDIUM': 2, 'HIGH': 3}[level.value])
        if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
            risk = RiskLevel.HIGH
        intent = IntentType(_payload(intent_artifact)['intent'])
        if risk != RiskLevel.LOW:
            intent = IntentType.RISK if risk == RiskLevel.HIGH else IntentType.CONSULT
    except (KeyError, ValueError, TypeError):
        pass
    ids = {'intent': intent_artifact.id if intent_artifact else None,
           'risk': risk_artifact.id if risk_artifact else None,
           'context': context.id if context else None}
    normal = intent == IntentType.CHAT and risk == RiskLevel.LOW
    retrieved = _payload(context).get('retrievedKnowledge', [])
    evidence_valid = isinstance(retrieved, list) and all(
        isinstance(getattr(item, 'source', None), str) and bool(item.source.strip())
        and isinstance(getattr(item, 'content', None), str) and bool(item.content.strip()) for item in retrieved)
    evidence_mode = 'general_chat' if normal else 'retrieved_support' if retrieved else 'generic_support'
    messages = payload.get('messages')
    valid_messages = isinstance(messages, list) and bool(messages) and all(
        isinstance(m, AiMessage) and m.role in {'system', 'user', 'assistant'}
        and isinstance(m.content, str) and bool(m.content.strip()) for m in messages)
    current = board.model_input or board.user_input
    safe_policy = risk is not None and payload.get('safetyPolicy') == reply_policy(risk.value)
    safe_policy_message = bool(valid_messages and risk is not None and messages[0] == reply_policy_message(risk.value))
    checks = {
        'assessmentsPresent': bool(intent_artifact and risk_artifact and intent and risk and assessed
                                   and getattr(assessed, 'risk', None) == risk),
        'currentArtifactBindings': bool(response and context and payload.get('inputArtifactIds') == ids),
        'routeMatchesAssessment': bool(response and response.owner == 'ResponseAgent' and intent and risk
                                      and payload.get('intent') == intent.value and payload.get('risk') == risk.value
                                      and payload.get('mode') == ('normal_chat' if normal else 'support')),
        'messagesAndPolicyPrepared': bool(valid_messages and current and messages[-1].role == 'user'
                                          and messages[-1].content == current and safe_policy and safe_policy_message),
        'evidenceModeDeclared': bool(context and evidence_valid and payload.get('evidenceMode') == evidence_mode),
        'clarificationPrepared': bool(not getattr(assessed, 'needs_clarification', False)
                                      or (valid_messages and CLARIFICATION_MESSAGE in messages)),
        'independentReviewBound': bool(response and review and review.owner == 'SafetyAgent'
                                      and checked.get('scope') == 'prompt_plan_only'
                                      and checked.get('responseArtifactId') == response.id
                                      and review.metadata.get('responseArtifactId') == response.id
                                      and risk and checked.get('risk') == risk.value),
        'reviewPassed': bool(checked.get('approved') is True
                             and isinstance(checked.get('checks'), dict)
                             and checked['checks'].get('serverPolicyMatches') is True
                             and checked['checks'].get('serverPolicyMessageMatches') is True),
    }
    return PlanValidation(all(checks.values()), checks,
                          tuple(key for key, passed in checks.items() if not passed), ids, evidence_mode)
