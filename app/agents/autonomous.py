from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.agents.events import (
    AgentArtifact,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentTask,
    AgentTurnResult,
    CollaborationBlackboard,
    TaskPriority,
)
from app.agents.registry import AgentCapability, AgentDecision, AgentProfile
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal
from app.services.assessment import PsychologicalAssessmentService
from app.services.risk_signals import analyze_risk, has_personal_support_signal
from app.services.intent_contract import INTENT_CONTRACT, parse_intent
from app.services.reply_safety import reply_policy, reply_policy_message
from app.services.query_rewrite import QueryRewriteResult, rewrite_query
from app.agents.plan_validation import CLARIFICATION_MESSAGE
from app.services.turn_budget import current_turn, checkpoint

if TYPE_CHECKING:
    from app.models.entities import ChatSession, UserAccount
    from app.services.knowledge import KnowledgeService, SearchResult
    from app.services.memory import RedisShortTermMemoryStore


GENERAL_TASK_TOPICS = [
    "java", "python", "javascript", "代码", "编程", "程序", "算法", "数据库", "spring", "maven",
    "前端", "后端", "项目", "接口", "bug", "报错", "作业", "论文", "翻译", "天气", "路线",
]
GENERAL_TASK_WORDS = [
    *GENERAL_TASK_TOPICS, "总结", "解释", "怎么写", "如何", "是什么", "为什么", "给我", "帮我", "推荐", "查询",
]


@dataclass
class AgentRuntimeServices:
    db: Session
    settings: Settings
    user: UserAccount
    session: ChatSession
    ai: AiClient
    model_registry: AgentModelRegistry
    memory: RedisShortTermMemoryStore
    private_memory: "AgentPrivateMemory"
    knowledge: KnowledgeService
    history: list[AiMessage] = field(default_factory=list)
    previous_risk: str | None = None


class AgentPrivateMemory:
    """Per-agent memory facade backed by isolated Redis keys."""

    def __init__(self, settings: Settings):
        from app.services.memory import RedisShortTermMemoryStore

        self.store = RedisShortTermMemoryStore(settings)

    def load(self, agent_name: str, session_public_id: str) -> list[AiMessage]:
        return self.store.load_recent(self._key(agent_name, session_public_id))

    def append(self, agent_name: str, session_public_id: str, content: str) -> None:
        self.store.append(self._key(agent_name, session_public_id), "system", content)

    def _key(self, agent_name: str, session_public_id: str) -> str:
        return f"agent:{agent_name}:{session_public_id}"


class BaseAutonomousAgent:
    profile: AgentProfile

    def __init__(self, services: AgentRuntimeServices):
        self.services = services

    @property
    def name(self) -> str:
        return self.profile.name

    def client(self) -> AiClient:
        return self.services.model_registry.client_for(self.name)

    def private_memory(self) -> list[AiMessage]:
        checkpoint()
        return self.services.private_memory.load(self.name, self.services.session.public_id)

    def remember(self, content: str) -> None:
        checkpoint()
        self.services.private_memory.append(self.name, self.services.session.public_id, content)

    def _artifact(
        self,
        kind: str,
        payload: dict[str, Any],
        task: AgentTask,
        metadata: dict[str, Any] | None = None,
    ) -> AgentArtifact:
        return AgentArtifact(
            id=f"{self.name}:{kind}:{uuid.uuid4().hex[:10]}",
            owner=self.name,
            kind=kind,
            payload=payload,
            task_id=task.id,
            metadata=metadata or {},
        )


class UnderstandingAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="UnderstandingAgent",
        capabilities=frozenset({AgentCapability.UNDERSTANDING}),
        system_prompt=(
            '你是 UnderstandingAgent。只返回JSON对象 {"intent":"CHAT"}，'
            "intent只能为CHAT、CONSULT、RISK，不输出主题、置信度、理由或其他字段。"
            "不生成最终回复，不执行风险处置。"
        ),
        memory_policy="private_intent_history",
        model_profile="understanding",
        tool_permissions=frozenset({"llm.intent"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        if board.latest_artifact("intent"):
            return AgentDecision(False, reason="intent artifact already exists")
        if self._is_directed(task, board):
            return AgentDecision(True, 0.82, "open user-turn task needs understanding")
        return AgentDecision(False, reason="task does not need understanding")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        classification = self._classify(board.model_input or board.user_input, board)
        intent = classification.intent
        payload = {
            "intent": intent.value,
            "topic": self._topic(board.model_input or board.user_input),
            "reason": classification.reason,
            "source": classification.source,
            "degraded": classification.source == "fallback",
            "errorType": classification.error_type,
            "contract": INTENT_CONTRACT,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        self.remember(f"intent={intent.value}; topic={payload['topic']}")
        return AgentTurnResult(
            artifacts=(self._artifact("intent", payload, task),),
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="*",
                    task_id=task.id,
                    kind="PROPOSAL",
                    content=f"我判断本轮意图是 {intent.value}",
                ),
            ),
        )

    def _is_directed(self, task: AgentTask, board: CollaborationBlackboard) -> bool:
        if AgentCapability.UNDERSTANDING.value in task.required_capabilities:
            return True
        return bool(board.user_input and task.metadata.get("kind") in {"root", "understanding"})

    def _classify(self, text: str, board: CollaborationBlackboard) -> "IntentClassification":
        lowered = text.lower()
        signal = analyze_risk(text, self.services.history, self.services.previous_risk)
        if signal.level == RiskLevel.HIGH:
            return IntentClassification(IntentType.RISK, "rule", signal.reason)
        if signal.level == RiskLevel.MEDIUM:
            return IntentClassification(IntentType.CONSULT, "rule", signal.reason)
        if signal.reference_only:
            return IntentClassification(IntentType.CHAT, "rule", signal.reason)
        if signal.negated_only:
            intent = IntentType.CONSULT if has_consult_signal(lowered) or self.services.previous_risk == "HIGH" else IntentType.CHAT
            return IntentClassification(intent, "rule", signal.reason)
        if has_personal_support_signal(text):
            return IntentClassification(IntentType.CONSULT, "rule", "current_personal_support_request")
        # Request verbs occur in both ordinary tasks and emotional disclosures.
        # Only domain topics can shortcut a model classification.
        if not has_consult_signal(lowered) and any(word in lowered for word in GENERAL_TASK_TOPICS):
            return IntentClassification(IntentType.CHAT, "rule", "ordinary_task_shortcut")
        try:
            memory_context = "\n".join(item.content for item in self.private_memory()[-6:])
            messages = [
                *PromptTemplates.intent_prompt(self.services.history, text),
                AiMessage(role="system", content=f"{self.profile.system_prompt}\n私有记忆：\n{memory_context or '无'}"),
            ]
            raw = self.client().complete(messages, json_mode=self.services.settings.intent_json_mode)
            intent = parse_intent(raw)
            return IntentClassification(intent, "model", "validated_intent_json")
        except Exception as exc:
            intent = IntentType.CONSULT if has_consult_signal(lowered) else IntentType.CHAT
            return IntentClassification(intent, "fallback", "intent_unavailable_or_invalid", type(exc).__name__)

    def _topic(self, text: str) -> str:
        lowered = text.lower()
        if has_high_risk_signal(lowered):
            return "safety"
        if has_consult_signal(lowered):
            return "mental_health_support"
        if any(word in lowered for word in GENERAL_TASK_WORDS):
            return "general_task"
        return "conversation"


@dataclass(frozen=True)
class IntentClassification:
    intent: IntentType
    source: str
    reason: str
    error_type: str | None = None


class SafetyAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="SafetyAgent",
        capabilities=frozenset({AgentCapability.SAFETY}),
        system_prompt=(
            "你是 SafetyAgent。你独立评估风险，并审查候选回复是否安全。"
            "你可以发布 SAFETY_OVERRIDE；你不生成最终回复。"
        ),
        memory_policy="private_safety_ledger",
        model_profile="safety",
        tool_permissions=frozenset({"llm.risk", "rules.high_risk", "response.review"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        latest_response = board.latest_artifact("response_proposal")
        latest_review = board.latest_artifact("plan_review")
        if task.metadata.get("kind") == "plan_review" and latest_response and (latest_review is None or latest_review.metadata.get("responseArtifactId") != latest_response.id):
            return AgentDecision(True, 0.95, "candidate prompt plan needs validation")
        if task.metadata.get("kind") == "risk" and not board.latest_artifact("risk") and board.user_input:
            claim_priority = 0.98 if has_high_risk_signal(board.user_input) else 0.84
            return AgentDecision(True, claim_priority, "user input needs independent risk assessment")
        return AgentDecision(False, reason="no safety work needed")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        response = board.latest_artifact("response_proposal")
        review = board.latest_artifact("plan_review")
        if task.metadata.get("kind") == "plan_review" and response and (review is None or review.metadata.get("responseArtifactId") != response.id):
            return self._review_response(task, board, response)
        return self._assess_risk(task, board)

    def _assess_risk(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        assessment = PsychologicalAssessmentService(self.client()).assess(
            board.model_input or board.user_input, self.services.history, self.services.previous_risk,
        )
        control = current_turn()
        if control: control.note_risk(assessment.risk.value)
        payload = {
            "risk": assessment.risk.value,
            "emotion": assessment.emotion.value,
            "emotionScore": assessment.emotion_score,
            "confidenceInfo": assessment.confidence_info,
            "summary": assessment.summary,
            "assessment": assessment,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        events: tuple[AgentEvent, ...] = ()
        if assessment.risk == RiskLevel.HIGH:
            events = (
                AgentEvent(
                    type=AgentEventType.SAFETY_OVERRIDE,
                    actor=self.name,
                    task_id=task.id,
                    message="RiskGuardian hard/LLM assessment raised this turn to HIGH",
                    metadata={"risk": RiskLevel.HIGH.value},
                ),
            )
        self.remember(f"risk={assessment.risk.value}; summary={assessment.summary}")
        return AgentTurnResult(
            artifacts=(self._artifact("risk", payload, task),),
            events=events,
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="CoordinatorAgent",
                    task_id=task.id,
                    kind="SAFETY_ASSESSMENT",
                    content=f"risk={assessment.risk.value}",
                ),
            ),
        )

    def _review_response(self, task: AgentTask, board: CollaborationBlackboard, response: AgentArtifact) -> AgentTurnResult:
        risk = _risk_level(board)
        messages = response.payload.get("messages", [])
        expected = reply_policy_message(risk.value)
        checks = {
            "serverPolicyMatches": response.payload.get("safetyPolicy") == reply_policy(risk.value),
            "serverPolicyMessageMatches": bool(messages and messages[0] == expected),
        }
        approved = all(checks.values())
        reason = "server-authored preparation constraints validated; actual reply body still requires review" if approved else "reply plan lacks matching server-authored policy"
        payload = {
            "approved": approved,
            "reason": reason,
            "responseArtifactId": response.id,
            "risk": risk.value,
            "scope": "prompt_plan_only",
            "checks": checks,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        kind = "plan_review" if approved else "critique"
        events = ()
        follow_up_tasks = ()
        if not approved:
            events = (
                AgentEvent(
                    type=AgentEventType.REVISION_REQUESTED,
                    actor=self.name,
                    task_id=task.id,
                    artifact_id=response.id,
                    message=reason,
                ),
            )
            follow_up_tasks = (
                AgentTask(
                    id=f"task:revise-response:{uuid.uuid4().hex[:8]}",
                    title="Revise unsafe response proposal",
                    description=reason,
                    priority=TaskPriority.CRITICAL,
                    required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
                    created_by=self.name,
                    metadata={"kind": "response", "revisionOf": response.id},
                ),
            )
        self.remember(f"review approved={approved}; reason={reason}")
        return AgentTurnResult(
            artifacts=(self._artifact(kind, payload, task, {"responseArtifactId": response.id}),),
            tasks=follow_up_tasks,
            events=events,
        )


class ContextAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="ContextAgent",
        capabilities=frozenset({AgentCapability.CONTEXT}),
        system_prompt=(
            "你是 ContextAgent。你只负责为本轮协作提供上下文，包括私有记忆、会话摘要、RAG 证据和 skill 约束。"
            "你不判断最终答案是否可采纳。"
        ),
        memory_policy="private_context_memory",
        model_profile="context",
        tool_permissions=frozenset({"redis.memory", "mysql.messages", "rag.retrieve", "skills.read"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        if board.latest_artifact("context"):
            return AgentDecision(False, reason="context artifact already exists")
        risk = _risk_level(board)
        intent = _intent(board)
        if AgentCapability.CONTEXT.value in task.required_capabilities:
            return AgentDecision(True, 0.86, "task explicitly asks for context")
        if risk in {RiskLevel.MEDIUM, RiskLevel.HIGH} or intent in {IntentType.CONSULT, IntentType.RISK}:
            return AgentDecision(True, 0.82, "support path needs memory, RAG, and skill context")
        return AgentDecision(False, reason="context not necessary for current artifacts")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        from app.services.skills import MindBridgeSkillLibrary

        memory = board.latest_artifact("memory")
        memory_brief = memory.payload["memoryBrief"]
        model_history = memory.payload["modelHistory"]
        intent = _intent(board)
        risk = _risk_level(board)

        retrieved: list["SearchResult"] = []
        query = ""
        skill_context = ""
        rewrite = None
        if intent != IntentType.CHAT or risk != RiskLevel.LOW:
            rewrite = self._rewrite_query(memory_brief, board.model_input)
            query = rewrite.query
            retrieved = self.services.knowledge.retrieve(query, self.services.settings.knowledge_top_k)
            skill_context = MindBridgeSkillLibrary.response_skill_context(intent, risk, board.user_input)
        payload = {
            "memoryBrief": memory_brief,
            "modelHistory": model_history,
            "knowledgeQuery": query,
            "queryRewrite": rewrite.metadata() if rewrite else {
                "source": "skipped", "degraded": False, "reasonCode": "REWRITE_NOT_NEEDED"},
            "retrievedKnowledge": retrieved,
            "skillContext": skill_context,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        self.remember(f"context intent={intent.value}; risk={risk.value}; retrieved={len(retrieved)}")
        return AgentTurnResult(
            artifacts=(self._artifact("context", payload, task),),
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="ResponseAgent",
                    task_id=task.id,
                    kind="CONTEXT_READY",
                    content=f"context ready; retrieved={len(retrieved)}",
                ),
            ),
        )

    def _rewrite_query(self, memory_brief: str, model_input: str) -> QueryRewriteResult:
        return rewrite_query(self.client(), [
                AiMessage(role="system", content=f"{self.profile.system_prompt}\n把学生输入改写成适合检索校园心理知识库的中文查询词，只输出查询词。"),
                AiMessage(role="user", content=f"记忆摘要：\n{memory_brief}\n\n当前输入：\n{model_input}"),
            ], model_input)



class ResponseAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="ResponseAgent",
        capabilities=frozenset({AgentCapability.RESPONSE}),
        system_prompt=(
            "你是 ResponseAgent。根据意图、风险、上下文和安全约束组织面向学生的候选回复，"
            "直接回应学生需求；候选需要独立审核后才能发送。"
        ),
        memory_policy="private_response_strategy",
        model_profile="response",
        tool_permissions=frozenset({"llm.response_plan"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        if board.latest_artifact("response_proposal") and "revisionOf" not in task.metadata:
            return AgentDecision(False, reason="response proposal already exists")
        if not board.latest_artifact("intent") or not board.latest_artifact("risk"):
            return AgentDecision(False, reason="response needs intent and risk artifacts")
        intent = _intent(board)
        risk = _risk_level(board)
        if intent == IntentType.CHAT and risk == RiskLevel.LOW:
            return AgentDecision(True, 0.78, "normal chat response can be proposed")
        if board.latest_artifact("context") or risk == RiskLevel.HIGH:
            return AgentDecision(True, 0.84, "support response has enough artifacts")
        if AgentCapability.RESPONSE.value in task.required_capabilities:
            return AgentDecision(True, 0.65, "explicit response task")
        return AgentDecision(False, reason="waiting for context")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        intent = _intent(board)
        risk = _risk_level(board)
        context = board.latest_artifact("context")
        memory = board.latest_artifact("memory")
        context_payload = context.payload if context else memory.payload if memory else {}
        model_history = context_payload.get("modelHistory") or [AiMessage(role="user", content=board.model_input)]
        memory_brief = context_payload.get("memoryBrief") or "无相关历史记忆。"
        knowledge = context_payload.get("retrievedKnowledge") or []
        skill_context = context_payload.get("skillContext") or ""
        knowledge_context = "\n\n".join(f"- [{item.source}] {item.content}" for item in knowledge)
        if intent == IntentType.CHAT and risk == RiskLevel.LOW:
            messages = [
                PromptTemplates.answer_system_prompt(IntentType.CHAT, RiskLevel.LOW, "", '学生'),
                AiMessage(
                    role="system",
                    content=(
                        f"{self.profile.system_prompt}\n"
                        f"当前由 ResponseAgent 以 normal_chat mode 提出回复方案。\n"
                        f"私有记忆：\n{_format_private_memory(self.private_memory())}\n"
                        f"记忆摘要：\n{memory_brief}"
                    ),
                ),
                *model_history,
            ]
            mode = "normal_chat"
        else:
            messages = [
                PromptTemplates.answer_system_prompt(
                    intent if intent != IntentType.CHAT else IntentType.CONSULT,
                    risk,
                    knowledge_context,
                    '学生',
                    skill_context,
                ),
                AiMessage(
                    role="system",
                    content=(
                        f"{self.profile.system_prompt}\n"
                        f"当前由 ResponseAgent 以 support mode 提出回复方案。\n"
                        f"私有记忆：\n{_format_private_memory(self.private_memory())}\n"
                        f"记忆摘要：\n{memory_brief}"
                    ),
                ),
                *model_history,
            ]
            mode = "support"
        payload = {
            "messages": messages,
            "mode": mode,
            "intent": intent.value,
            "risk": risk.value,
            "responseAgent": self.name,
            "inputArtifactIds": {
                "intent": board.latest_artifact("intent").id,
                "risk": board.latest_artifact("risk").id,
                "context": (context or memory).id if (context or memory) else None,
            },
            "evidenceMode": "general_chat" if mode == "normal_chat" else "retrieved_support" if knowledge else "generic_support",
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        assessment = board.latest_artifact("risk")
        if assessment and assessment.payload["assessment"].needs_clarification:
            messages.insert(1, CLARIFICATION_MESSAGE)
        messages.insert(0, reply_policy_message(risk.value))
        payload["safetyPolicy"] = reply_policy(risk.value)
        self.remember(f"response mode={mode}; intent={intent.value}; risk={risk.value}")
        return AgentTurnResult(
            artifacts=(self._artifact("response_proposal", payload, task),),
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="SafetyAgent",
                    task_id=task.id,
                    kind="REVIEW_REQUEST",
                    content="请审查候选回复方案。",
                ),
            ),
        )


class CoordinatorAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="CoordinatorAgent",
        capabilities=frozenset({AgentCapability.COORDINATION}),
        system_prompt=(
            "你是 CoordinatorAgent。你不规定固定 Agent 顺序；你只维护任务板、预算、安全门槛、冲突仲裁和最终采纳。"
        ),
        memory_policy="private_coordination_trace",
        model_profile="coordinator",
        tool_permissions=frozenset({"taskboard.write", "blackboard.accept"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        return AgentDecision(False, reason="CoordinatorAgent is driven by the event loop, not by fixed workflow slots")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        return AgentTurnResult(close_task=False)

    def root_task(self, board: CollaborationBlackboard) -> AgentTask:
        return AgentTask(
            id="task:root",
            title="Resolve user turn",
            description=board.user_input,
            priority=TaskPriority.CRITICAL if has_high_risk_signal(board.user_input) else TaskPriority.NORMAL,
            created_by=self.name,
            required_capabilities=frozenset({AgentCapability.COORDINATION.value}),
            metadata={"kind": "root"},
        )

    def remember_acceptance(self, artifact_id: str, reason: str) -> None:
        self.remember(f"accepted={artifact_id}; reason={reason}")


def _intent(board: CollaborationBlackboard) -> IntentType:
    risk = _risk_level(board)
    if risk == RiskLevel.HIGH:
        return IntentType.RISK
    if risk == RiskLevel.MEDIUM:
        return IntentType.CONSULT
    artifact = board.latest_artifact("intent")
    if artifact:
        try:
            return IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
        except ValueError:
            return IntentType.CHAT
    if has_high_risk_signal(board.user_input):
        return IntentType.RISK
    if has_consult_signal(board.user_input):
        return IntentType.CONSULT
    return IntentType.CHAT


def _risk_level(board: CollaborationBlackboard) -> RiskLevel:
    highest = RiskLevel.LOW
    order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
    for artifact in board.artifacts_by_kind("risk"):
        try:
            risk = RiskLevel(str(artifact.payload.get("risk", RiskLevel.LOW.value)).upper())
        except ValueError:
            risk = RiskLevel.LOW
        if order[risk] > order[highest]:
            highest = risk
    if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
        return RiskLevel.HIGH
    return highest


def _context_history(board: CollaborationBlackboard) -> list[AiMessage]:
    context = board.latest_artifact("context")
    if not context:
        return [AiMessage(role="user", content=board.model_input or board.user_input)]
    return context.payload.get("modelHistory") or [AiMessage(role="user", content=board.model_input or board.user_input)]


def _format_private_memory(items: list[AiMessage]) -> str:
    if not items:
        return "无"
    return "\n".join(f"- {item.content}" for item in items[-5:])
