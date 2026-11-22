"""Contratos del ciclo Dev ↔ QA: `DevOutput` y `QAReport`."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from specforge.core.errors import ContractViolationError
from specforge.core.transition_guard import TransitionEvent
from specforge.schemas.blueprint import TestId
from specforge.schemas.common import Contract, QualifiedRef, ShortText, ensure_unique

if TYPE_CHECKING:
    from specforge.schemas.blueprint import ArchitectureBlueprint


def _relative_path(value: str) -> str:
    if ".." in value.split("/"):
        raise ValueError(f"ruta fuera del repositorio: {value}")
    return value


# Ruta relativa al repositorio (sin `/` inicial ni segmentos `..`).
FilePath = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_.-][A-Za-z0-9_./-]*$", max_length=200),
    AfterValidator(_relative_path),
]
IssueId = Annotated[str, StringConstraints(pattern=r"^QA-[0-9]{2}$")]

MAX_QA_ERRORS = 10
_QA_ERROR_MAX_LEN = 240


class FileChangeType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"


class FileChange(Contract):
    path: FilePath
    change_type: FileChangeType
    summary: ShortText


class DevOutput(Contract):
    blueprint_version: int = Field(ge=1)
    attempt: int = Field(ge=1, le=4)
    summary: ShortText
    files: list[FileChange] = Field(min_length=1, max_length=60)
    implemented: list[QualifiedRef] = Field(min_length=1, max_length=60)
    addressed_issues: list[IssueId] = Field(default_factory=list, max_length=30)
    notes: list[ShortText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        ensure_unique([f.path for f in self.files], "archivos")
        if self.attempt > 1 and not self.addressed_issues:
            raise ValueError("un reintento debe declarar qué issues de QA atiende")
        return self

    def check_against(self, blueprint: ArchitectureBlueprint) -> None:
        if self.blueprint_version != blueprint.blueprint_version:
            raise ContractViolationError(
                [f"DevOutput v{self.blueprint_version} ≠ blueprint v{blueprint.blueprint_version}"]
            )


class Verdict(StrEnum):
    PASS = "PASS"  # noqa: S105 - valor de dominio, no una contraseña
    FAIL = "FAIL"


class TestStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class IssueSeverity(StrEnum):
    BLOCKER = "BLOCKER"
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


class TestResult(Contract):
    test_id: TestId
    status: TestStatus
    detail: ShortText | None = None


class QAIssue(Contract):
    id: IssueId
    severity: IssueSeverity
    ref: QualifiedRef | None = None
    description: ShortText
    suggested_fix: ShortText | None = None

    @property
    def is_release_blocking(self) -> bool:
        return self.severity in {IssueSeverity.BLOCKER, IssueSeverity.CRITICAL}


class QAReport(Contract):
    blueprint_version: int = Field(ge=1)
    attempt: int = Field(ge=1, le=4)
    verdict: Verdict
    results: list[TestResult] = Field(min_length=1, max_length=80)
    issues: list[QAIssue] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def _verdict_is_consistent(self) -> Self:
        ensure_unique([r.test_id for r in self.results], "resultados")
        ensure_unique([i.id for i in self.issues], "issues")
        failing = [r.test_id for r in self.results if r.status is TestStatus.FAILED]
        blocking = [i.id for i in self.issues if i.is_release_blocking]
        if self.verdict is Verdict.PASS and (failing or blocking):
            raise ValueError(
                f"verdict PASS incompatible con tests fallidos {failing} o issues {blocking}"
            )
        if self.verdict is Verdict.FAIL and not (failing or blocking):
            raise ValueError("verdict FAIL requiere tests fallidos o issues bloqueantes")
        return self

    def check_against(self, blueprint: ArchitectureBlueprint) -> None:
        """Cada test planificado debe tener resultado y no se aceptan tests desconocidos."""
        planned = {t.id for t in blueprint.test_plan}
        reported = {r.test_id for r in self.results}
        violations = [f"{t} planificado sin resultado" for t in sorted(planned - reported)]
        violations += [f"{t} no está en el plan de pruebas" for t in sorted(reported - planned)]
        if self.blueprint_version != blueprint.blueprint_version:
            violations.append(
                f"QAReport v{self.blueprint_version} ≠ blueprint v{blueprint.blueprint_version}"
            )
        if violations:
            raise ContractViolationError(violations)

    @property
    def transition_event(self) -> TransitionEvent:
        return (
            TransitionEvent.QA_PASSED if self.verdict is Verdict.PASS else TransitionEvent.QA_FAILED
        )

    def to_qa_errors(self) -> list[str]:
        """Resumen compacto para la memoria episódica (`FeatureDeliveryState.qa_errors`)."""
        order = list(IssueSeverity)
        lines = [
            f"{i.id} [{i.severity}] {i.ref or '-'}: {i.description}"
            + (f" → {i.suggested_fix}" if i.suggested_fix else "")
            for i in sorted(self.issues, key=lambda i: order.index(i.severity))
        ]
        lines += [
            f"{r.test_id} FAILED: {r.detail or 'sin detalle'}"
            for r in self.results
            if r.status is TestStatus.FAILED
        ]
        return [line[:_QA_ERROR_MAX_LEN] for line in lines[:MAX_QA_ERRORS]]
