"""Estado del grafo y contratos Human-in-the-Loop (payload de `interrupt` y valores de resume).

El checkpoint de LangGraph guarda solo `GraphState`: el hilo al que pertenece la ejecución
y el `FeatureDeliveryState` serializado (punteros y contadores). Los artefactos viven en
PostgreSQL y se resuelven por puntero.
"""

from __future__ import annotations

from typing import Any, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from specforge.core.enums import ApprovalDecision, ApprovalGate
from specforge.core.transition_guard import ChangeTarget
from specforge.schemas.state import FeatureDeliveryState

GATE_DECISIONS = (
    ApprovalDecision.APPROVED,
    ApprovalDecision.CHANGES_REQUESTED,
    ApprovalDecision.REJECTED,
)


class GraphState(TypedDict):
    run_thread: str
    delivery: dict[str, Any]


def encode(state: FeatureDeliveryState) -> dict[str, Any]:
    return state.model_dump(mode="json")


def decode(graph_state: GraphState) -> FeatureDeliveryState:
    return FeatureDeliveryState.model_validate(graph_state["delivery"])


class QuestionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    question: str
    severity: str
    story_ids: list[str] = Field(default_factory=list)


class InterruptPayload(BaseModel):
    """Lo que ve el humano mientras el hilo está pausado."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interrupt_id: str
    gate: ApprovalGate
    feature_id: str
    thread_id: str
    phase: str
    blueprint_version: int
    artifact_id: str | None = None
    reason: str
    summary: str = ""
    questions: list[QuestionView] = Field(default_factory=list)
    allowed_decisions: list[ApprovalDecision]


class _Resume(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    reviewer: str = Field(min_length=1, max_length=120)
    comments: str = Field(default="", max_length=4000)


class GateResume(_Resume):
    """Decisión en HUMAN_GATE: aprobar, pedir cambios (al blueprint o a las historias) o
    rechazar."""

    decision: ApprovalDecision
    target: ChangeTarget = ChangeTarget.BLUEPRINT
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("decision")
    @classmethod
    def _gate_decision(cls, value: ApprovalDecision) -> ApprovalDecision:
        if value not in GATE_DECISIONS:
            raise ValueError(f"decisión inválida para BLUEPRINT_APPROVAL: {value.value}")
        return value

    @model_validator(mode="after")
    def _changes_need_detail(self) -> Self:
        if (
            self.decision is ApprovalDecision.CHANGES_REQUESTED
            and not self.comments
            and not self.payload
        ):
            raise ValueError("pedir cambios exige comentarios o payload")
        return self


class ClarificationResume(_Resume):
    """Respuestas a preguntas críticas (o rechazo de la feature)."""

    answers: dict[str, str] = Field(default_factory=dict)
    reject: bool = False

    @model_validator(mode="after")
    def _answers_or_reject(self) -> Self:
        if not self.reject and not self.answers:
            raise ValueError("responde al menos una pregunta o rechaza la feature")
        return self
