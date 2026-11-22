"""Historias de usuario con criterios de aceptación y condiciones de satisfacción.

El dominio se construye de forma progresiva: el PO entrega un `IntakeBrief` con historias
borrador y el BUSINESS_EXPERT las refina en sucesivos `StoryRefinementOutput`. La evolución
entre versiones es verificable de forma determinista con `validate_progression`.

Agnosticismo al giro: cualquier valor operativo (intervalos de recordatorio, contadores de
uso, canales, umbrales) se declara como `ConfigurableParameter` y las reglas deterministas
solo pueden referenciarlo por clave; nunca se codifica el valor de un rubro concreto.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, JsonValue, StringConstraints, model_validator

from specforge.core.errors import StoryProgressionError
from specforge.schemas.common import (
    ConditionId,
    ConfigKey,
    Contract,
    CriterionId,
    ExceptionId,
    LongText,
    QuestionId,
    RuleId,
    ShortText,
    StoryId,
    ensure_unique,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

ModuleCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]{1,15}$")]


class StoryStatus(StrEnum):
    DRAFT = "DRAFT"
    REFINED = "REFINED"
    READY = "READY"


class ParameterScope(StrEnum):
    GLOBAL = "GLOBAL"
    BUSINESS_PROFILE = "BUSINESS_PROFILE"
    TENANT = "TENANT"


class ParameterType(StrEnum):
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    DURATION = "DURATION"
    STRING = "STRING"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"
    LIST = "LIST"


class ConditionKind(StrEnum):
    FUNCTIONAL = "FUNCTIONAL"
    NON_FUNCTIONAL = "NON_FUNCTIONAL"
    COMPLIANCE = "COMPLIANCE"


class QuestionSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


class StoryChangeType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"


class ConfigurableParameter(Contract):
    """Parámetro de negocio resuelto en runtime desde `business_profiles.config`."""

    key: ConfigKey
    value_type: ParameterType
    scope: ParameterScope = ParameterScope.BUSINESS_PROFILE
    description: ShortText
    default: JsonValue = None
    allowed_values: list[ShortText] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def _enum_needs_values(self) -> Self:
        if self.value_type is ParameterType.ENUM and not self.allowed_values:
            raise ValueError(f"el parámetro ENUM {self.key} requiere allowed_values")
        return self


class AcceptanceCriterion(Contract):
    id: CriterionId
    given: ShortText
    when: ShortText
    then: ShortText


class SatisfactionCondition(Contract):
    id: ConditionId
    kind: ConditionKind = ConditionKind.FUNCTIONAL
    statement: ShortText


class DeterministicRule(Contract):
    """Regla que se implementa en código/SQL, nunca se delega al LLM."""

    id: RuleId
    statement: ShortText
    parameter_keys: list[ConfigKey] = Field(default_factory=list, max_length=10)


class StoryException(Contract):
    id: ExceptionId
    condition: ShortText
    expected_behavior: ShortText
    rule_ids: list[RuleId] = Field(default_factory=list, max_length=10)


class OpenQuestion(Contract):
    id: QuestionId
    question: ShortText
    severity: QuestionSeverity
    story_ids: list[StoryId] = Field(default_factory=list, max_length=10)
    answer: ShortText | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity is QuestionSeverity.CRITICAL and self.answer is None


class UserStoryWithConditions(Contract):
    story_id: StoryId
    title: ShortText
    as_a: ShortText
    i_want: ShortText
    so_that: ShortText
    status: StoryStatus = StoryStatus.DRAFT
    revision: int = Field(default=1, ge=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list, max_length=15)
    satisfaction_conditions: list[SatisfactionCondition] = Field(
        default_factory=list, max_length=15
    )
    deterministic_rules: list[DeterministicRule] = Field(default_factory=list, max_length=15)
    exceptions: list[StoryException] = Field(default_factory=list, max_length=15)
    configurable_parameters: list[ConfigurableParameter] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        ensure_unique([c.id for c in self.acceptance_criteria], f"{self.story_id}: criterios")
        ensure_unique([c.id for c in self.satisfaction_conditions], f"{self.story_id}: condiciones")
        ensure_unique([r.id for r in self.deterministic_rules], f"{self.story_id}: reglas")
        ensure_unique([e.id for e in self.exceptions], f"{self.story_id}: excepciones")
        ensure_unique([p.key for p in self.configurable_parameters], f"{self.story_id}: parámetros")

        param_keys = {p.key for p in self.configurable_parameters}
        for rule in self.deterministic_rules:
            unknown = sorted(set(rule.parameter_keys) - param_keys)
            if unknown:
                raise ValueError(
                    f"{self.story_id}/{rule.id} usa parámetros no declarados: {', '.join(unknown)}"
                )
        rule_ids = {r.id for r in self.deterministic_rules}
        for exc in self.exceptions:
            unknown = sorted(set(exc.rule_ids) - rule_ids)
            if unknown:
                raise ValueError(
                    f"{self.story_id}/{exc.id} referencia reglas inexistentes: {', '.join(unknown)}"
                )
        if self.status is StoryStatus.READY and not (
            self.acceptance_criteria and self.satisfaction_conditions
        ):
            raise ValueError(
                f"{self.story_id} READY requiere criterios de aceptación"
                " y condiciones de satisfacción"
            )
        return self

    def refs(self) -> frozenset[str]:
        """Referencias calificadas (`US-001/AC-01`, …) de todos sus elementos."""
        items = [
            *(c.id for c in self.acceptance_criteria),
            *(c.id for c in self.satisfaction_conditions),
            *(r.id for r in self.deterministic_rules),
            *(e.id for e in self.exceptions),
        ]
        return frozenset(f"{self.story_id}/{item}" for item in items)

    def acceptance_refs(self) -> frozenset[str]:
        return frozenset(f"{self.story_id}/{c.id}" for c in self.acceptance_criteria)

    def fingerprint(self) -> str:
        """Hash del contenido sin `revision` (detecta cambios reales entre versiones)."""
        payload = self.model_dump(mode="json", exclude={"revision"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _check_question_refs(questions: Sequence[OpenQuestion], story_ids: set[str]) -> None:
    ensure_unique([q.id for q in questions], "preguntas")
    for question in questions:
        unknown = sorted(set(question.story_ids) - story_ids)
        if unknown:
            raise ValueError(
                f"{question.id} referencia historias inexistentes: {', '.join(unknown)}"
            )


class IntakeBrief(Contract):
    """Salida del PO en INTAKE_PO: alcance inicial e historias borrador."""

    title: ShortText
    problem_statement: LongText
    module: ModuleCode
    business_profile: ShortText | None = None
    scope_in: list[ShortText] = Field(min_length=1, max_length=15)
    scope_out: list[ShortText] = Field(default_factory=list, max_length=15)
    assumptions: list[ShortText] = Field(default_factory=list, max_length=15)
    draft_stories: list[UserStoryWithConditions] = Field(min_length=1, max_length=12)
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=15)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        ids = [s.story_id for s in self.draft_stories]
        ensure_unique(ids, "historias")
        _check_question_refs(self.open_questions, set(ids))
        return self

    @property
    def blocking_questions(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.is_blocking]

    @property
    def requires_clarification(self) -> bool:
        return bool(self.blocking_questions)


class StoryChange(Contract):
    story_id: StoryId
    change_type: StoryChangeType
    rationale: ShortText


class StoryRefinementOutput(Contract):
    """Salida del BUSINESS_EXPERT en STORY_REFINEMENT (conjunto completo y vigente)."""

    stories: list[UserStoryWithConditions] = Field(min_length=1, max_length=20)
    changes: list[StoryChange] = Field(default_factory=list, max_length=40)
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=15)
    refinement_notes: list[ShortText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        ids = {s.story_id for s in self.stories}
        ensure_unique([s.story_id for s in self.stories], "historias")
        ensure_unique([c.story_id for c in self.changes], "cambios por historia")
        for change in self.changes:
            present = change.story_id in ids
            if change.change_type is StoryChangeType.REMOVED and present:
                raise ValueError(f"{change.story_id} marcada REMOVED pero sigue en stories")
            if change.change_type is not StoryChangeType.REMOVED and not present:
                raise ValueError(f"{change.story_id} marcada {change.change_type} pero no existe")
        _check_question_refs(self.open_questions, ids)
        return self

    @property
    def blocking_questions(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.is_blocking]

    @property
    def ready_for_blueprint(self) -> bool:
        return not self.blocking_questions and all(
            s.status is StoryStatus.READY for s in self.stories
        )

    def acceptance_refs(self) -> frozenset[str]:
        return frozenset().union(*(s.acceptance_refs() for s in self.stories))


def validate_progression(
    previous: Sequence[UserStoryWithConditions], refinement: StoryRefinementOutput
) -> None:
    """Verifica que el refinamiento evolucione las historias previas de forma trazable.

    - Una historia previa que desaparece debe declararse REMOVED.
    - Una historia nueva debe declararse ADDED y empezar en `revision=1`.
    - Una historia con contenido distinto debe declararse MODIFIED y subir `revision` en 1.
    - Una historia sin cambios conserva su `revision` y no se declara MODIFIED.
    - Una historia READY no puede volver a DRAFT.
    """
    before = {s.story_id: s for s in previous}
    after = {s.story_id: s for s in refinement.stories}
    declared = {c.story_id: c.change_type for c in refinement.changes}
    violations: list[str] = []

    for story_id in sorted(before.keys() - after.keys()):
        if declared.get(story_id) is not StoryChangeType.REMOVED:
            violations.append(f"{story_id} desapareció sin declararse REMOVED")

    for story_id in sorted(after.keys() - before.keys()):
        story = after[story_id]
        if declared.get(story_id) is not StoryChangeType.ADDED:
            violations.append(f"{story_id} es nueva y no se declaró ADDED")
        if story.revision != 1:
            violations.append(f"{story_id} es nueva y debe tener revision=1")

    for story_id in sorted(before.keys() & after.keys()):
        old, new = before[story_id], after[story_id]
        changed = old.fingerprint() != new.fingerprint()
        if changed:
            if declared.get(story_id) is not StoryChangeType.MODIFIED:
                violations.append(f"{story_id} cambió y no se declaró MODIFIED")
            if new.revision != old.revision + 1:
                violations.append(
                    f"{story_id} cambió: revision debe ser {old.revision + 1} (es {new.revision})"
                )
        else:
            if declared.get(story_id) is not None:
                violations.append(f"{story_id} declarada {declared[story_id]} sin cambios reales")
            if new.revision != old.revision:
                violations.append(f"{story_id} sin cambios no debe alterar revision")
        if old.status is StoryStatus.READY and new.status is StoryStatus.DRAFT:
            violations.append(f"{story_id} no puede volver de READY a DRAFT")

    if violations:
        raise StoryProgressionError(violations)
