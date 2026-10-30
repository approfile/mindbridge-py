
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.result import AgentRunResult
from app.models.entities import AgentRunTrace, ChatSession, UserAccount
from app.services.observability import bind_ids, correlation, current_observation, log_event, timed


class AgentTraceService:
    def __init__(self, db: Session):
        self.db = db

    def append_reply_events(self, trace_id: int, events: list[dict]) -> None:
        trace = self.db.get(AgentRunTrace, trace_id)
        if trace is None:
            raise ValueError("Trace not found")
        steps = json.loads(trace.agent_steps_json)
        trace.agent_steps_json = _json([*steps, *events])
        self.db.commit()

    def append_observation(self, trace_id: int, observation, *, turn_control=None) -> None:
        trace = self.db.get(AgentRunTrace, trace_id)
        if trace is None:
            raise ValueError('Trace not found')
        # The final snapshot already includes every closed planning span.
        entries = [entry for entry in json.loads(trace.agent_steps_json)
                   if entry.get('kind') != 'observability' and (turn_control is None or entry.get('kind') != 'turn_control')]
        if turn_control is not None:
            entries.append({'kind': 'turn_control', **turn_control})
        trace.agent_steps_json = _json([*entries, {'kind': 'observability', 'data': observation.snapshot()}])
        self.db.commit()

    @timed('persistence.trace')
    def save_run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        sanitized_input: str,
        memory_brief: str,
        agent_run: AgentRunResult,
        report_id: int | None,
    ) -> AgentRunTrace:
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=report_id,
            intent=agent_run.intent.value,
            risk_level=agent_run.risk_level.value,
            original_input=original_input,
            sanitized_input=sanitized_input,
            memory_brief=memory_brief,
            agent_steps_json=_json(_agent_steps_with_collaboration(agent_run)),
            retrieved_knowledge_json=_json(agent_run.retrieved_knowledge),
            response_messages_json=_json(agent_run.response_messages),
            assessment_json=_json(agent_run.assessment or {}),
        )
        self.db.add(trace)
        self.db.flush()
        bind_ids(trace_id=trace.id)
        entries = json.loads(trace.agent_steps_json)
        entries.append({'kind': 'run_correlation', **correlation()})
        observation = current_observation()
        if observation:
            entries.append({'kind': 'observability', 'data': observation.snapshot('planning')})
        trace.agent_steps_json = _json(entries)
        self.db.commit()
        self.db.refresh(trace)
        log_event('agent.run.persisted')
        return trace


def _json(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, default=str)


def _agent_steps_with_collaboration(agent_run: AgentRunResult) -> list[Any]:
    entries: list[Any] = [*agent_run.steps]
    entries.extend(
        {
            "kind": "agent_event",
            "type": getattr(event.type, "value", event.type),
            "actor": event.actor,
            "taskId": event.task_id,
            "artifactId": event.artifact_id,
            "message": event.message,
            "metadata": event.metadata,
        }
        for event in agent_run.collaboration_events
    )
    entries.extend(
        {
            "kind": "agent_task",
            "id": task.id,
            "title": task.title,
            "status": getattr(task.status, "value", task.status),
            "priority": getattr(task.priority, "value", task.priority),
            "requiredCapabilities": sorted(task.required_capabilities),
            "claimedBy": list(task.claimed_by),
            "createdBy": task.created_by,
            "metadata": task.metadata,
        }
        for task in agent_run.collaboration_tasks
    )
    entries.extend(
        {
            "kind": "agent_artifact",
            "id": artifact.id,
            "owner": artifact.owner,
            "artifactKind": artifact.kind,
            "taskId": artifact.task_id,
            "metadata": artifact.metadata,
            "payload": artifact.payload,
        }
        for artifact in agent_run.collaboration_artifacts
    )
    return entries


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
