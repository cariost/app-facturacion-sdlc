"""Enrutamiento determinista de cada tarea a un tier de modelo (FAST o REASONING)."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from specforge.core.enums import AgentRole, ModelTier

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specforge.core.config import Settings


@dataclass(frozen=True, slots=True)
class ModelSpec:
    provider: str
    name: str
    tier: ModelTier
    max_output_tokens: int
    effort: str | None = None


@dataclass(frozen=True, slots=True)
class RouteRequest:
    role: AgentRole
    node: str
    context_tokens: int
    attempt: int = 1  # intento Dev ↔ QA (retry_count + 1)
    repair: int = 0  # reparaciones tras una salida inválida
    force_tier: ModelTier | None = None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    spec: ModelSpec
    reason: str


# Tier base por rol: lo rutinario (intake, verificación contra un plan) va al modelo barato;
# refinamiento de reglas, diseño e implementación requieren razonamiento.
BASE_TIERS: Mapping[AgentRole, ModelTier] = MappingProxyType(
    {
        AgentRole.PO: ModelTier.FAST,
        AgentRole.BUSINESS_EXPERT: ModelTier.REASONING,
        AgentRole.ARCHITECT_TECH_LEAD: ModelTier.REASONING,
        AgentRole.DEV: ModelTier.REASONING,
        AgentRole.QA: ModelTier.FAST,
        AgentRole.SYSTEM: ModelTier.FAST,
    }
)


class RoutingPolicy:
    def __init__(
        self,
        *,
        fast: ModelSpec,
        reasoning: ModelSpec,
        escalate_above_tokens: int,
        base_tiers: Mapping[AgentRole, ModelTier] = BASE_TIERS,
    ) -> None:
        if fast.tier is not ModelTier.FAST or reasoning.tier is not ModelTier.REASONING:
            raise ValueError("los modelos deben declararse con su tier correspondiente")
        self._models = {ModelTier.FAST: fast, ModelTier.REASONING: reasoning}
        self._escalate_above = escalate_above_tokens
        self._base = base_tiers

    @classmethod
    def from_settings(cls, settings: Settings) -> RoutingPolicy:
        return cls(
            fast=ModelSpec(
                provider=settings.llm_provider,
                name=settings.model_fast,
                tier=ModelTier.FAST,
                max_output_tokens=settings.llm_max_output_tokens,
                effort=settings.effort_fast,
            ),
            reasoning=ModelSpec(
                provider=settings.llm_provider,
                name=settings.model_reasoning,
                tier=ModelTier.REASONING,
                max_output_tokens=settings.llm_max_output_tokens,
                effort=settings.effort_reasoning,
            ),
            escalate_above_tokens=settings.routing_escalate_above_tokens,
        )

    def model(self, tier: ModelTier) -> ModelSpec:
        return self._models[tier]

    def route(self, request: RouteRequest) -> RouteDecision:
        if request.force_tier is not None:
            return RouteDecision(self._models[request.force_tier], "tier forzado por el nodo")
        if request.role not in self._base:
            raise ValueError(f"el rol {request.role.value} no invoca modelos")
        base = self._base[request.role]
        if base is ModelTier.REASONING:
            return RouteDecision(self._models[base], f"rol {request.role.value} → REASONING")
        if request.repair > 0:
            return RouteDecision(
                self._models[ModelTier.REASONING], "escalado: reparación de salida inválida"
            )
        if request.context_tokens > self._escalate_above:
            return RouteDecision(
                self._models[ModelTier.REASONING],
                f"escalado: contexto ~{request.context_tokens} > {self._escalate_above} tokens",
            )
        if request.attempt > 1:
            return RouteDecision(
                self._models[ModelTier.REASONING],
                f"escalado: intento {request.attempt} tras fallo de QA",
            )
        return RouteDecision(self._models[base], f"rol {request.role.value} → FAST")
