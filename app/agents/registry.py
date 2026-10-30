from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
import math

from app.agents.events import AgentTask, AgentTurnResult, CollaborationBlackboard


class AgentCapability(str, Enum):
    UNDERSTANDING = "UNDERSTANDING"
    SAFETY = "SAFETY"
    CONTEXT = "CONTEXT"
    RESPONSE = "RESPONSE"
    COORDINATION = "COORDINATION"


@dataclass(frozen=True)
class AgentProfile:
    name: str
    capabilities: frozenset[AgentCapability] = field(default_factory=frozenset)
    system_prompt: str = ""
    memory_policy: str = "none"
    model_profile: str = "default"
    tool_permissions: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AgentDecision:
    claim: bool
    claim_priority: float = 0.0
    reason: str = ""

    def __post_init__(self):
        # Deterministic relative scheduling preference, never correctness.
        if type(self.claim_priority) not in {float, int} or not math.isfinite(self.claim_priority) or not 0 <= self.claim_priority <= 1:
            raise ValueError('claim_priority must be a finite scheduling preference in [0,1]')


class AutonomousAgent(Protocol):
    profile: AgentProfile

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        ...

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        ...


@dataclass(frozen=True)
class AgentCandidate:
    agent: AutonomousAgent
    decision: AgentDecision


class AgentRegistry:
    def __init__(self, agents: list[AutonomousAgent]):
        self._agents = list(agents)

    @property
    def agents(self) -> list[AutonomousAgent]:
        return list(self._agents)

    def candidates_for(self, task: AgentTask, board: CollaborationBlackboard) -> list[AutonomousAgent]:
        return [candidate.agent for candidate in self.candidate_decisions_for(task, board)]

    def candidate_decisions_for(self, task: AgentTask, board: CollaborationBlackboard) -> list[AgentCandidate]:
        candidates = []
        for agent in self._agents:
            if not self._has_required_capability(agent, task):
                continue
            decision = agent.decide(task, board)
            if decision.claim:
                candidates.append(AgentCandidate(agent, decision))
        return sorted(candidates, key=lambda item: item.decision.claim_priority, reverse=True)

    def _has_required_capability(self, agent: AutonomousAgent, task: AgentTask) -> bool:
        if not task.required_capabilities:
            return True
        agent_capabilities = {capability.value for capability in agent.profile.capabilities}
        return set(task.required_capabilities).issubset(agent_capabilities)
