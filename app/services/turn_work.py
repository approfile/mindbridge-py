"""Each blocking operation creates/uses/closes its own Session in one worker."""
from copy import deepcopy
from dataclasses import replace

from sqlalchemy.orm import sessionmaker

from app.agents.harness import MindBridgeAgentHarness, SessionReference
from app.models.entities import ChatSession, UserAccount
from app.services.trace import AgentTraceService
from app.services.turn_budget import checkpoint
from app.services.risk_signals import current_signal
from app.services.observability import log_event


class OwnedTurnWork:
    def __init__(self, engine, settings, user_id, request, control, observation, runtime_factory=None):
        self.sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.settings = settings; self.user_id = user_id; self.request = request
        self.control = control; self.observation = observation; self.runtime_factory = runtime_factory
        self.outcome = None

    def plan(self):
        checkpoint()
        signal = current_signal(self.request.message)
        if signal.level: self.control.note_risk(signal.level.value)
        with self.sessions() as db:
            user = db.get(UserAccount, self.user_id)
            if user is None or 'ROLE_ADMIN' in user.roles:
                raise ValueError('Student account not available')
            harness = MindBridgeAgentHarness(db, self.settings, runtime_factory=self.runtime_factory)
            # Expired optional work does not start a new session. Deterministic
            # observed danger still gets rule-only durable finalization.
            if self.control.reason and self.control.risk not in {'HIGH', 'MEDIUM'}:
                checkpoint()
            result = harness.run(user, self.request)
            # No ORM object, connection or lazy relationship leaves this scope.
            self.outcome = replace(result, session=SessionReference(result.session.id, result.session.public_id),
                                   assessment=deepcopy(result.assessment),
                                   response_messages=deepcopy(result.response_messages),
                                   retrieved_knowledge=deepcopy(result.retrieved_knowledge),
                                   agent_steps=deepcopy(result.agent_steps), memory_facts=deepcopy(result.memory_facts))
            return self.outcome

    def persist_reply(self, reply):
        checkpoint()
        with self.sessions() as db:
            user = db.get(UserAccount, self.user_id)
            session = db.query(ChatSession).filter_by(id=self.outcome.session.id, user_id=self.user_id).one()
            checkpoint()
            AgentTraceService(db).append_reply_events(self.outcome.trace_id, reply.events)
            checkpoint()
            harness = MindBridgeAgentHarness(db, self.settings)
            checkpoint()
            harness.save_assistant_message(user, session, reply.text)

    def finalize(self):
        # Finalization is tracked, model-free, and retains the admission slot.
        # Already-started commits are allowed to finish after request stop.
        if self.control.reason:
            self.observation.stream_status = self.control.status()
        trace_id = self.observation.ids.get('trace_id')
        try:
            if trace_id:
                with self.sessions() as db:
                    AgentTraceService(db).append_observation(trace_id, self.observation,
                                                             turn_control=self.control.snapshot())
        except Exception:
            log_event('turn.finalization_failed', status='failed', reason_code='TELEMETRY_PERSISTENCE_FAILED')
            raise
        finally:
            log_event('chat.completed', stream_status=self.observation.stream_status,
                      duration_ms=round(self.observation.elapsed_ms(), 3), reason_code=self.control.reason)
