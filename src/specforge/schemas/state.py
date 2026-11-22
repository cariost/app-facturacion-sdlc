"""`FeatureDeliveryState`: memoria de trabajo liviana que viaja por el grafo de LangGraph.

Solo contiene punteros (`artifact_ids`), contadores y resúmenes compactos. Está prohibido
cargar historiales o payloads de artefactos: el modelo rechaza campos extra y limita el
tamaño de cada texto, de modo que el checkpoint y el contexto por nodo se mantienen chicos.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from specforge.core.enums import ApprovalGate, ArtifactType, Phase
from specforge.core.ids import FeatureId, ThreadId
from specforge.schemas.common import ShortText

if TYPE_CHECKING:
    from specforge.core.transition_guard import TransitionDecision
    from specforge.db.models import Feature

EXECUTIVE_SUMMARY_MAX_LINES = 15
EXECUTIVE_SUMMARY_MAX_CHARS = 2000
MAX_ACTIVE_CONSTRAINTS = 20
MAX_QA_ERRORS = 10


class PendingInterrupt(BaseModel):
    """Pausa Human-in-the-Loop en curso (se resuelve con `Command(resume=...)`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interrupt_id: ShortText
    gate: ApprovalGate
    reason: ShortText
    question_ids: list[ShortText] = Field(default_factory=list, max_length=15)
    artifact_id: uuid.UUID | None = None


class FeatureDeliveryState(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Identidad
    feature_id: str
    tenant_id: ShortText
    module: str
    business_profile: str | None = None
    industry: str | None = None

    # Ciclo de vida (espejo de `features`; la BD es la fuente de verdad)
    phase: Phase = Phase.INTAKE_PO
    blocked_from_phase: Phase | None = None
    blueprint_version: int = Field(default=1, ge=1)
    run_seq: int = Field(default=1, ge=1)
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0, le=3)
    row_version: int = Field(default=1, ge=1)

    # Punteros a la última versión de cada artefacto relevante
    artifact_ids: dict[ArtifactType, uuid.UUID] = Field(default_factory=dict)

    # Memoria de trabajo compacta
    executive_summary: str = Field(default="", max_length=EXECUTIVE_SUMMARY_MAX_CHARS)
    active_constraints: list[ShortText] = Field(
        default_factory=list, max_length=MAX_ACTIVE_CONSTRAINTS
    )
    qa_errors: list[ShortText] = Field(default_factory=list, max_length=MAX_QA_ERRORS)
    pending_interrupt: PendingInterrupt | None = None
    last_error: ShortText | None = None

    @field_validator("feature_id")
    @classmethod
    def _valid_feature_id(cls, value: str) -> str:
        FeatureId.parse(value)
        return value

    @field_validator("executive_summary")
    @classmethod
    def _compact_summary(cls, value: str) -> str:
        if len(value.splitlines()) > EXECUTIVE_SUMMARY_MAX_LINES:
            raise ValueError(f"executive_summary excede {EXECUTIVE_SUMMARY_MAX_LINES} líneas")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if FeatureId.parse(self.feature_id).module != self.module:
            raise ValueError("module no coincide con el prefijo de feature_id")
        if self.retry_count > self.max_retries:
            raise ValueError("retry_count supera max_retries")
        if (self.phase is Phase.BLOCKED) != (self.blocked_from_phase is not None):
            raise ValueError("blocked_from_phase solo se define en BLOCKED")
        return self

    @classmethod
    def from_feature(cls, feature: Feature, *, industry: str | None = None) -> Self:
        """Construye la memoria de trabajo desde la fila de `features` (fuente de verdad)."""
        return cls(
            feature_id=feature.feature_id,
            tenant_id=feature.tenant_id,
            module=feature.module,
            business_profile=feature.business_profile,
            industry=industry,
            phase=Phase(feature.phase),
            blocked_from_phase=Phase(feature.blocked_from_phase)
            if feature.blocked_from_phase
            else None,
            blueprint_version=feature.blueprint_version,
            run_seq=feature.run_seq,
            retry_count=feature.retry_count,
            max_retries=feature.max_retries,
            row_version=feature.row_version,
            executive_summary=feature.executive_summary,
            active_constraints=list(feature.active_constraints),
        )

    @property
    def thread_id(self) -> str:
        return str(ThreadId(FeatureId.parse(self.feature_id), self.blueprint_version, self.run_seq))

    def artifact_id(self, artifact_type: ArtifactType) -> uuid.UUID | None:
        return self.artifact_ids.get(artifact_type)

    def with_artifact(
        self, artifact_type: ArtifactType, artifact_id: uuid.UUID
    ) -> dict[str, object]:
        """Actualización parcial para devolver desde un nodo de LangGraph."""
        return {"artifact_ids": {**self.artifact_ids, artifact_type: artifact_id}}

    def after_transition(
        self, decision: TransitionDecision, *, row_version: int
    ) -> dict[str, object]:
        """Actualización parcial que refleja localmente una transición ya persistida.

        Reproduce los efectos que aplica el trigger de BD (reintento, nueva versión de
        blueprint con reinicio de contadores, fase de bloqueo).
        """
        update: dict[str, object] = {
            "phase": decision.to_phase,
            "row_version": row_version,
            "blocked_from_phase": decision.from_phase
            if decision.to_phase is Phase.BLOCKED
            else None,
        }
        if decision.count_retry:
            update["retry_count"] = self.retry_count + 1
        if decision.new_blueprint_version:
            update |= {
                "blueprint_version": self.blueprint_version + 1,
                "run_seq": 1,
                "retry_count": 0,
                "qa_errors": [],
            }
        return update
