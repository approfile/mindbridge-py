"""Evaluation-only fixed-order workflow using the same policies and model profiles.

This is not a one-call LLM agent. It isolates scheduling overhead from capability
changes; production factory routing is intentionally unchanged.
"""
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.agents.autonomous import UnderstandingAgent, SafetyAgent, ContextAgent, ResponseAgent
from app.agents.events import AgentEvent, AgentEventType, AgentTask
from app.agents.registry import AgentCapability
from app.core.enums import IntentType, RiskLevel
from app.agents.plan_validation import validate_plan


class SequentialWorkflowBaseline(EventDrivenAgentRuntimeService):
    def run(self, user, session, original_input, model_input):
        services, board = self.prepare_turn(user, session, original_input, model_input)
        schedule = [
            ('intent', UnderstandingAgent, AgentCapability.UNDERSTANDING),
            ('risk', SafetyAgent, AgentCapability.SAFETY),
            ('context', ContextAgent, AgentCapability.CONTEXT),
            ('response_proposal', ResponseAgent, AgentCapability.RESPONSE),
            ('plan_review', SafetyAgent, AgentCapability.SAFETY),
        ]
        steps = 0
        for kind, agent_type, capability in schedule:
            if kind == 'context' and self._select_intent(board) == IntentType.CHAT and self._select_risk(board) == RiskLevel.LOW:
                continue
            if steps >= self.settings.agent_max_rounds:
                break
            task = AgentTask(id='baseline:' + kind, title=kind,
                             required_capabilities=frozenset({capability.value}), metadata={'kind': kind})
            agent = agent_type(services)
            board = board.add_task(task).apply_turn_result(task, agent.name, agent.act(task, board))
            steps += 1
        response = board.latest_artifact('response_proposal')
        review = board.latest_artifact('plan_review')
        validation = validate_plan(board)
        if validation.approved:
            board = board.accept_plan(response.id, 'SequentialCoordinator',
                                      'fixed-order plan contract accepted; body review still required', validation.to_dict())
        else:
            board = board.append_event(AgentEvent(type=AgentEventType.BUDGET_EXHAUSTED, actor='SequentialCoordinator'))
        return self._to_result(board, user)
