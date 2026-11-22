"""Subagentes PO, BUSINESS_EXPERT, ARCHITECT_TECH_LEAD, DEV y QA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from specforge.agents.architect import ArchitectAgent
from specforge.agents.base import (
    Agent,
    AgentDeps,
    AgentOutcome,
    AgentResult,
    Clarification,
    PreparedTask,
)
from specforge.agents.business_expert import BusinessExpertAgent
from specforge.agents.developer import DeveloperAgent
from specforge.agents.product_owner import ProductOwnerAgent
from specforge.agents.qa import QAAgent

if TYPE_CHECKING:
    from specforge.core.enums import Phase


@dataclass(frozen=True, slots=True)
class AgentSuite:
    product_owner: ProductOwnerAgent
    business_expert: BusinessExpertAgent
    architect: ArchitectAgent
    developer: DeveloperAgent
    qa: QAAgent

    @classmethod
    def build(cls, deps: AgentDeps) -> AgentSuite:
        return cls(
            product_owner=ProductOwnerAgent(deps),
            business_expert=BusinessExpertAgent(deps),
            architect=ArchitectAgent(deps),
            developer=DeveloperAgent(deps),
            qa=QAAgent(deps),
        )

    def all(self) -> tuple[Agent[Any], ...]:
        return (self.product_owner, self.business_expert, self.architect, self.developer, self.qa)

    def for_phase(self, phase: Phase) -> Agent[Any]:
        for agent in self.all():
            if agent.phase is phase:
                return agent
        raise KeyError(f"ningún agente atiende la fase {phase.value}")


__all__ = [
    "Agent",
    "AgentDeps",
    "AgentOutcome",
    "AgentResult",
    "AgentSuite",
    "ArchitectAgent",
    "BusinessExpertAgent",
    "Clarification",
    "DeveloperAgent",
    "PreparedTask",
    "ProductOwnerAgent",
    "QAAgent",
]
