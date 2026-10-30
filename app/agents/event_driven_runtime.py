from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.autonomous import (
    AgentPrivateMemory,
    AgentRuntimeServices,
    ContextAgent,
    CoordinatorAgent,
    ResponseAgent,
    SafetyAgent,
    UnderstandingAgent,
)
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, AgentEvent, AgentEventType, CollaborationBlackboard
from app.agents.registry import AgentRegistry
from app.agents.result import AgentRunResult, AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import AiClient, PromptTemplates
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.memory import RedisShortTermMemoryStore
from app.services.session_memory import SessionMemoryService
from app.services.assessment import PsychologicalAssessmentService
from app.services.observability import correlation, timed
from app.services.turn_budget import TurnStopped, checkpoint, current_turn
from app.models.entities import PsychologicalReport


class EventDrivenAgentRuntimeService:
    """Actor-style multi-agent runtime.

    Agents observe open tasks, claim work independently, and return the shared
    AgentRunResult contract consumed by the rest of the app.
    """

    framework_name = "event_driven_multi_agent"
    max_steps = 8

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.knowledge = KnowledgeService(db, settings)
        self.memory = RedisShortTermMemoryStore(settings)
        self.model_registry = AgentModelRegistry(settings)
        self.private_memory = AgentPrivateMemory(settings)

    def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str) -> AgentRunResult:
        board = None; scheduler = None
        try:
            checkpoint()
            services, board = self.prepare_turn(user, session, original_input, model_input)
            checkpoint()
            coordinator = CoordinatorAgent(services)
            agents = [UnderstandingAgent(services), SafetyAgent(services), ContextAgent(services), ResponseAgent(services)]
            scheduler = EventDrivenCoordinator(AgentRegistry(agents), coordinator, self.settings)
            final = scheduler.run(board)
            checkpoint()
            return self._to_result(final, user)
        except TurnStopped as exc:
            return interrupted_result(self.db, user, session, original_input, model_input, exc.reason,
                                      scheduler.latest_board if scheduler else board)

    @timed('memory.load')
    def prepare_turn(self, user, session, original_input, model_input):
        snapshot = SessionMemoryService(self.db, self.settings, self.memory).load(user, session, model_input)
        services = AgentRuntimeServices(
            db=self.db,
            settings=self.settings,
            user=user,
            session=session,
            ai=self.ai,
            model_registry=self.model_registry,
            memory=self.memory,
            private_memory=self.private_memory,
            knowledge=self.knowledge,
            history=snapshot.history,
            previous_risk=snapshot.previous_risk,
        )
        board = CollaborationBlackboard(
            turn_id=correlation().get('run_id') or uuid.uuid4().hex,
            user_id=user.id,
            session_id=session.public_id,
            user_input=original_input,
            model_input=model_input,
        )
        board = board.add_artifact(AgentArtifact(
            id="memory:" + uuid.uuid4().hex, owner="SessionMemoryService", kind="memory", task_id="memory:load",
            payload={"history": snapshot.history,
                     "modelHistory": [*snapshot.model_history, AiMessage(role="user", content=model_input)],
                     "memoryBrief": snapshot.brief, "source": snapshot.source,
                     "factSources": snapshot.facts, "lastMessageId": snapshot.last_message_id,
                     "previousRisk": snapshot.previous_risk},
        ))
        board = board.append_event(
            AgentEvent(
                type=AgentEventType.TURN_STARTED,
                actor="CoordinatorAgent",
                message="user turn published to shared task board",
            )
        )
        return services, board

    def _to_result(self, board: CollaborationBlackboard, user: UserAccount) -> AgentRunResult:
        intent = self._select_intent(board)
        risk = self._select_risk(board)
        context = board.latest_artifact("context") or board.latest_artifact("memory")
        risk_artifact = board.latest_artifact("risk")
        accepted = board.accepted_artifact()
        memory_brief = "无相关历史记忆。"
        retrieved: list[SearchResult] = []
        response_messages: list[AiMessage] = []
        if context:
            memory_brief = context.payload.get("memoryBrief") or memory_brief
            retrieved = context.payload.get("retrievedKnowledge") or []
        if accepted:
            response_messages = accepted.payload.get("messages") or []
        assessment = risk_artifact.payload.get("assessment") if risk_artifact else None
        exhausted = any(event.type == AgentEventType.BUDGET_EXHAUSTED for event in board.events)
        if assessment is None:
            memory = board.latest_artifact("memory")
            # Exhaustion must not make a fresh, unbudgeted model call or discard
            # an explicit crisis. Rule fallback can still persist its outbox.
            assessment = PsychologicalAssessmentService.rule_fallback(
                board.model_input, memory.payload["history"] if memory else [],
                memory.payload.get("previousRisk") if memory else None,
            )
            risk = assessment.risk
        if risk == RiskLevel.HIGH:
            intent = IntentType.RISK
        elif risk == RiskLevel.MEDIUM:
            intent = IntentType.CONSULT
        return AgentRunResult(
            intent=intent,
            risk_level=risk,
            assessment=assessment,
            retrieved_knowledge=retrieved,
            response_messages=response_messages,
            steps=self._events_to_steps(board),
            memory_brief=memory_brief,
            collaboration_events=list(board.events),
            collaboration_tasks=list(board.tasks.values()),
            collaboration_artifacts=list(board.artifacts),
            plan_accepted=accepted is not None,
            budget_exhausted=exhausted,
        )

    def _select_intent(self, board: CollaborationBlackboard) -> IntentType:
        if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
            return IntentType.RISK
        artifact = board.latest_artifact("intent")
        if not artifact:
            return IntentType.CHAT
        try:
            return IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
        except ValueError:
            return IntentType.CHAT

    def _select_risk(self, board: CollaborationBlackboard) -> RiskLevel:
        order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
        highest = RiskLevel.LOW
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

    def _events_to_steps(self, board: CollaborationBlackboard) -> list[AgentStep]:
        steps = []
        for index, event in enumerate(board.events, start=1):
            detail = event.message or _compact_json(event.metadata)
            if event.artifact_id:
                detail = f"{detail}; artifact={event.artifact_id}" if detail else f"artifact={event.artifact_id}"
            steps.append(AgentStep(index, event.actor, event.type.value, detail))
        return steps


def _compact_json(value: Any) -> str:
    jsonable = _to_jsonable(value)
    if not jsonable:
        return ""
    return str(jsonable)[:240]


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


def interrupted_result(db, user, session, original_input, model_input, reason, board=None):
    """Rule-only durable safety finalization; never calls a model after stop."""
    if board is None:
        board = CollaborationBlackboard(turn_id=correlation().get('run_id') or uuid.uuid4().hex,
                                        user_id=user.id, session_id=session.public_id,
                                        user_input=original_input, model_input=model_input)
    memory = board.latest_artifact('memory')
    history = memory.payload.get('history', []) if memory else []
    previous = memory.payload.get('previousRisk') if memory else None
    if memory is None:
        report = db.query(PsychologicalReport).filter_by(user_id=user.id, session_id=session.id).order_by(PsychologicalReport.id.desc()).first()
        previous = report.risk_level if report else None
    fallback = PsychologicalAssessmentService.rule_fallback(model_input, history, previous)
    risk_artifact = board.latest_artifact('risk')
    assessment = risk_artifact.payload.get('assessment') if risk_artifact else None
    order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
    if assessment is None or order[fallback.risk] > order[assessment.risk]:
        assessment = fallback
    control = current_turn()
    if control and control.risk and order[RiskLevel(control.risk)] > order[assessment.risk]:
        from dataclasses import replace
        from app.core.enums import EmotionLabel
        # The stop signal preserves already observed safety severity, not a
        # late model response or an invented confidence probability.
        assessment = replace(fallback, risk=RiskLevel(control.risk),
                             emotion=EmotionLabel.HIGH_RISK if control.risk == 'HIGH' else EmotionLabel.ANXIETY,
                             emotion_score=4. if control.risk == 'HIGH' else 3.1,
                             needs_clarification=True,
                             reason_codes=(*fallback.reason_codes, 'PRESERVED_OBSERVED_RISK'))
    if control:
        control.note_risk(assessment.risk.value)
    intent_artifact = board.latest_artifact('intent')
    try:
        intent = IntentType(intent_artifact.payload['intent']) if intent_artifact else IntentType.CHAT
    except (ValueError, KeyError):
        intent = IntentType.CHAT
    if assessment.risk == RiskLevel.HIGH: intent = IntentType.RISK
    elif assessment.risk == RiskLevel.MEDIUM: intent = IntentType.CONSULT
    events = [*board.events,
              AgentEvent(type=AgentEventType.TURN_INTERRUPTED, actor='TurnBudget', message=reason,
                         metadata={'reasonCode': reason, 'planningAccepted': False}),
              AgentEvent(type=AgentEventType.BUDGET_EXHAUSTED, actor='TurnBudget', message=reason)]
    return AgentRunResult(intent=intent, risk_level=assessment.risk, assessment=assessment,
                          retrieved_knowledge=[], response_messages=[],
                          steps=[AgentStep(i, e.actor, e.type.value, e.message) for i, e in enumerate(events, 1)],
                          memory_brief=memory.payload.get('memoryBrief', '') if memory else '',
                          collaboration_events=events, collaboration_tasks=list(board.tasks.values()),
                          collaboration_artifacts=list(board.artifacts), plan_accepted=False, budget_exhausted=True)
