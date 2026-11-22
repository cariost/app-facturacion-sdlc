"""`ArchitectureBlueprint`: diseño del ARCHITECT_TECH_LEAD revisado en HUMAN_GATE."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from specforge.core.errors import ContractViolationError
from specforge.schemas.common import (
    Contract,
    LongText,
    QualifiedRef,
    ShortText,
    ensure_unique,
)

if TYPE_CHECKING:
    from specforge.schemas.stories import StoryRefinementOutput

ComponentName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]*$", max_length=80)]
TestId = Annotated[str, StringConstraints(pattern=r"^T-[0-9]{3}$")]
AdrId = Annotated[str, StringConstraints(pattern=r"^ADR-[0-9]{3}$")]
RiskId = Annotated[str, StringConstraints(pattern=r"^R-[0-9]{2}$")]


class Plane(StrEnum):
    """Separación obligatoria: reglas fiscales/operativas en el plano determinista."""

    DETERMINISTIC = "DETERMINISTIC"
    AGENTIC = "AGENTIC"


class ComponentKind(StrEnum):
    DOMAIN_SERVICE = "DOMAIN_SERVICE"
    API = "API"
    REPOSITORY = "REPOSITORY"
    SQL = "SQL"
    WORKER = "WORKER"
    INTEGRATION = "INTEGRATION"
    AGENT_NODE = "AGENT_NODE"
    UI = "UI"


class TestKind(StrEnum):
    UNIT = "UNIT"
    INTEGRATION = "INTEGRATION"
    CONTRACT = "CONTRACT"
    E2E = "E2E"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Component(Contract):
    name: ComponentName
    kind: ComponentKind
    plane: Plane
    responsibility: ShortText
    implements: list[QualifiedRef] = Field(default_factory=list, max_length=40)


class DataChange(Contract):
    table: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
    change: Annotated[str, StringConstraints(pattern=r"^(CREATE|ALTER|INDEX|SEED)$")]
    description: ShortText
    tenant_scoped: bool = True


class ApiEndpoint(Contract):
    method: Annotated[str, StringConstraints(pattern=r"^(GET|POST|PUT|PATCH|DELETE)$")]
    path: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9_{}/.-]*$", max_length=160)]
    purpose: ShortText
    idempotent: bool = False


class PlannedTest(Contract):
    id: TestId
    kind: TestKind
    description: ShortText
    covers: list[QualifiedRef] = Field(min_length=1, max_length=20)


class AdrRecord(Contract):
    id: AdrId
    title: ShortText
    decision: LongText
    alternatives: list[ShortText] = Field(default_factory=list, max_length=5)


class Risk(Contract):
    id: RiskId
    level: RiskLevel
    description: ShortText
    mitigation: ShortText


class ArchitectureBlueprint(Contract):
    blueprint_version: int = Field(ge=1)
    summary: LongText
    components: list[Component] = Field(min_length=1, max_length=30)
    data_changes: list[DataChange] = Field(default_factory=list, max_length=30)
    api_endpoints: list[ApiEndpoint] = Field(default_factory=list, max_length=30)
    test_plan: list[PlannedTest] = Field(min_length=1, max_length=60)
    adrs: list[AdrRecord] = Field(default_factory=list, max_length=10)
    risks: list[Risk] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        ensure_unique([c.name for c in self.components], "componentes")
        ensure_unique([t.id for t in self.test_plan], "tests")
        ensure_unique([a.id for a in self.adrs], "ADRs")
        ensure_unique([r.id for r in self.risks], "riesgos")
        ensure_unique([f"{e.method} {e.path}" for e in self.api_endpoints], "endpoints")
        return self

    def check_against(self, stories: StoryRefinementOutput) -> None:
        """Cobertura determinista blueprint ↔ historias vigentes.

        - Toda referencia debe existir en las historias.
        - Todo criterio de aceptación debe estar implementado por algún componente y
          cubierto por algún test planificado.
        - Las reglas deterministas (`RD-xx`) solo pueden implementarse en el plano
          DETERMINISTIC.
        """
        known = frozenset().union(*(s.refs() for s in stories.stories))
        acceptance = stories.acceptance_refs()
        violations: list[str] = []

        implemented: set[str] = set()
        for component in self.components:
            for ref in component.implements:
                if ref not in known:
                    violations.append(f"{component.name} implementa {ref} inexistente")
                if "/RD-" in ref and component.plane is not Plane.DETERMINISTIC:
                    violations.append(
                        f"{component.name} ({component.plane}) no puede implementar la regla "
                        f"determinista {ref}"
                    )
            implemented.update(component.implements)

        tested: set[str] = set()
        for test in self.test_plan:
            for ref in test.covers:
                if ref not in known:
                    violations.append(f"{test.id} cubre {ref} inexistente")
            tested.update(test.covers)

        violations.extend(
            f"{ref} sin componente que lo implemente" for ref in sorted(acceptance - implemented)
        )
        violations.extend(f"{ref} sin test planificado" for ref in sorted(acceptance - tested))
        if violations:
            raise ContractViolationError(violations)
