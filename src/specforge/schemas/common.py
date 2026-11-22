"""Base de los contratos estructurados que producen y consumen los agentes.

Todos los contratos son estrictos (`extra="forbid"`), inmutables y con límites de tamaño:
un LLM que devuelve campos inventados o textos desmesurados falla la validación en vez de
inflar el contexto de los nodos siguientes.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

if TYPE_CHECKING:
    from collections.abc import Iterable

# Límites de tamaño (presupuesto de tokens por campo).
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ConfigKey = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", max_length=120)
]

StoryId = Annotated[str, StringConstraints(pattern=r"^US-[0-9]{3}$")]
CriterionId = Annotated[str, StringConstraints(pattern=r"^AC-[0-9]{2}$")]
ConditionId = Annotated[str, StringConstraints(pattern=r"^CS-[0-9]{2}$")]
RuleId = Annotated[str, StringConstraints(pattern=r"^RD-[0-9]{2}$")]
ExceptionId = Annotated[str, StringConstraints(pattern=r"^EX-[0-9]{2}$")]
QuestionId = Annotated[str, StringConstraints(pattern=r"^Q-[0-9]{2}$")]
# Referencia calificada a un elemento de una historia: "US-001/AC-02".
QualifiedRef = Annotated[str, StringConstraints(pattern=r"^US-[0-9]{3}/(AC|CS|RD|EX)-[0-9]{2}$")]

SCHEMA_VERSION = "1"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def ensure_unique(values: Iterable[str], label: str) -> None:
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ValueError(f"{label} duplicados: {', '.join(duplicates)}")
