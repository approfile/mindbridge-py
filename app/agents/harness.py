from __future__ import annotations

import uuid
import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.result import AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, MessageRole
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer
from app.services.session_memory import SessionMemoryService
from app.services.tool_queue import ToolQueueService
from app.services.trace import AgentTraceService
from app.services.observability import bind_ids, observed_run, span, timed
from app.services.turn_budget import TurnStopped, checkpoint, current_turn


@dataclass
class AgentToolPlan:
    report_id: int | None
    risk_level: str | None

    @property
    def requires_tools(self) -> bool:
        return self.report_id is not None


@dataclass(frozen=True)
class SessionReference:
    id: int
    public_id: str


@dataclass
class AgentHarnessOutcome:
    session: ChatSession | SessionReference
    original_input: str
    model_input: str
    intent: IntentType
    risk_level: str | None
    assessment: PsychologyAssessment | None
    response_messages: list[AiMessage]
    agent_steps: list[AgentStep]
    retrieved_knowledge: list[SearchResult]
    report_id: int | None
    tool_plan: AgentToolPlan
    trace_id: int | None
    plan_accepted: bool = False
    budget_exhausted: bool = False
    memory_facts: dict = field(default_factory=dict)


class MindBridgeAgentHarness:
    """Runtime harness for one MindBridge agent turn.

    The harness owns business orchestration around the agent runtime. HTTP/SSE
    code can stay thin while this class manages input preparation, persistence,
    risk report creation, tool planning, and trace data.
    """

    def __init__(self, db: Session, settings: Settings, runtime_factory=None):
        self.db = db
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.memory = RedisShortTermMemoryStore(settings)
        self.runtime_factory = runtime_factory or create_agent_runtime

    @observed_run
    def run(self, user: UserAccount, request: ChatRequest) -> AgentHarnessOutcome:
        with span('input.prepare'):
            original_input = request.message.strip()
            model_input = self.privacy.sanitize(original_input)
            control = current_turn()
            if control:
                from app.services.risk_signals import current_signal
                signal = current_signal(model_input)
                if signal.level: control.note_risk(signal.level.value)
        with span('session.resolve'):
            session = self._resolve_session(user, request.sessionId, original_input)
            bind_ids(session_id=session.public_id)
            if control and control.lease:
                control.lease.bind_session(session.public_id)
        with span('planning.total'):
            try:
                checkpoint()
                agent_run = self.runtime_factory(self.db, self.settings).run(user, session, original_input, model_input)
                if not agent_run.budget_exhausted:
                    checkpoint()
            except TurnStopped as exc:
                from app.agents.event_driven_runtime import interrupted_result
                agent_run = interrupted_result(self.db, user, session, original_input, model_input, exc.reason)
        self.save_message(user, session, MessageRole.USER, original_input)

        report = self._create_report(user, session, original_input, agent_run)
        risk_level = report.risk_level if report is not None else None
        trace = AgentTraceService(self.db).save_run(
            user=user,
            session=session,
            original_input=original_input,
            sanitized_input=model_input,
            memory_brief=agent_run.memory_brief,
            agent_run=agent_run,
            report_id=report.id if report is not None else None,
        )
        tool_plan = AgentToolPlan(report_id=report.id if report is not None else None, risk_level=risk_level)
        return AgentHarnessOutcome(
            session=session,
            original_input=original_input,
            model_input=model_input,
            intent=agent_run.intent,
            risk_level=risk_level,
            assessment=agent_run.assessment,
            response_messages=agent_run.response_messages,
            agent_steps=agent_run.steps,
            retrieved_knowledge=agent_run.retrieved_knowledge,
            report_id=report.id if report is not None else None,
            tool_plan=tool_plan,
            trace_id=trace.id,
            plan_accepted=agent_run.plan_accepted,
            budget_exhausted=agent_run.budget_exhausted,
            memory_facts=next((artifact.payload.get('factSources', {}) for artifact in agent_run.collaboration_artifacts
                               if artifact.kind == 'memory'), {}),
        )

    @timed('persistence.assistant')
    def save_assistant_message(self, user: UserAccount, session: ChatSession, content: str) -> None:
        self.save_message(user, session, MessageRole.ASSISTANT, content)

    async def dispatch_tools(self, tool_plan: AgentToolPlan) -> list[str]:
        if tool_plan.report_id is None:
            return []
        # Compatibility entry point: normal runs already persist the outbox
        # before streaming. Repeated dispatch must not create duplicate tasks.
        ToolQueueService(self.db, self.settings).enqueue_report(tool_plan.report_id, tool_plan.risk_level)
        return ["queued"]

    @timed('persistence.message')
    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str) -> None:
        if self.db.query(ChatSession).filter_by(id=session.id, user_id=user.id).with_for_update().first() is None:
            raise ValueError("Session not found")
        message = ChatMessage(user_id=user.id, session_id=session.id, role=role.value, content=content)
        self.db.add(message)
        session.touch()
        self.db.add(session)
        self.db.flush()
        SessionMemoryService(self.db, self.settings, self.memory).synchronize(user, session)
        self.db.commit()
        self.memory.append(session.public_id, role.value, content, message_id=message.id)

    def _resolve_session(self, user: UserAccount, public_id: str | None, text: str) -> ChatSession:
        if public_id:
            session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id, ChatSession.user_id == user.id).first()
            if session is None:
                raise ValueError("Session not found")
            from app.services.data_retention import content_expired
            if content_expired(self.db,session,self.settings):
                raise ValueError('Session content retention expired; start a new conversation')
            return session
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=text[:36])
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    @timed('persistence.report_outbox')
    def _create_report(self, user: UserAccount, session: ChatSession, text: str, agent_run) -> PsychologicalReport | None:
        if not agent_run.requires_report or agent_run.assessment is None:
            return None
        report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content=text,
            intent=agent_run.intent.value,
            emotion=agent_run.assessment.emotion.value,
            emotion_score=agent_run.assessment.emotion_score,
            risk_level=agent_run.assessment.risk.value,
            confidence=agent_run.assessment.confidence,
            confidence_metadata_json=json.dumps(agent_run.assessment.confidence_info),
            summary=agent_run.assessment.summary,
        )
        self.db.add(report)
        try:
            self.db.flush()
            bind_ids(report_id=report.id)
            ToolQueueService(self.db, self.settings).enqueue_report(
                report.id, report.risk_level, commit=False,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(report)
        return report
