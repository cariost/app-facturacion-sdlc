"""Base de los subagentes.

Un agente no persiste ni transiciona: arma su tarea, invoca el `ModelRouter` con el contexto
mínimo de su nodo, valida la salida contra su contrato y reglas deterministas, y propone un
`AgentOutcome`. El nodo del grafo es quien guarda el artefacto y aplica la transición
decidida por el `TransitionGuard`, en una sola transacción.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from specforge.core.errors import ContextAssemblyError, ContractViolationError
from specforge.db.uow import unit_of_work
from specforge.llm.prompts import prompt_for
from specforge.schemas.common import Contract
from specforge.schemas.registry import artifact_type_for, load_as

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from specforge.context.builder import ContextBuilder, ContextBundle
    from specforge.core.enums import AgentRole, ApprovalGate, ArtifactType, Phase
    from specforge.core.transition_guard import TransitionEvent
    from specforge.db.engine import Database
    from specforge.db.models import FeatureArtifact
    from specforge.db.uow import UnitOfWork
    from specforge.llm.router import ModelRouter, RoutedResult
    from specforge.schemas.state import FeatureDeliveryState

T = TypeVar("T", bound=Contract)
C = TypeVar("C", bound=Contract)


@dataclass(frozen=True, slots=True)
class AgentDeps:
    db: Database
    context: ContextBuilder
    router: ModelRouter


@dataclass(frozen=True, slots=True)
class Clarification:
    gate: ApprovalGate
    question_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """Lo que el agente propone; el grafo lo convierte en persistencia + transición."""

    summary: str
    event: TransitionEvent | None = None
    clarification: Clarification | None = None
    # None = no tocar; lista = reemplazar los errores episódicos de QA del State.
    qa_errors: list[str] | None = None


@dataclass(frozen=True, slots=True)
class PreparedTask(Generic[T]):
    task: str
    validator: Callable[[T], None] | None = None


@dataclass(frozen=True, slots=True)
class AgentResult(Generic[T]):
    output: T
    routed: RoutedResult[T]
    bundle: ContextBundle
    extra: dict[str, object] = field(default_factory=dict)


def require(violations: Sequence[str]) -> None:
    if violations:
        raise ContractViolationError(list(violations))


async def find_latest(
    uow: UnitOfWork,
    state: FeatureDeliveryState,
    artifact_type: ArtifactType,
    cls: type[C],
    *,
    current_blueprint: bool = False,
) -> tuple[C, FeatureArtifact] | None:
    """Última versión de un artefacto rehidratada como contrato Pydantic (o None)."""
    artifact = await uow.artifacts.latest(
        state.feature_id,
        artifact_type,
        blueprint_version=state.blueprint_version if current_blueprint else None,
    )
    if artifact is None:
        return None
    return load_as(cls, artifact_type, artifact.schema_name, artifact.content), artifact


async def load_latest(
    uow: UnitOfWork,
    state: FeatureDeliveryState,
    artifact_type: ArtifactType,
    cls: type[C],
    *,
    current_blueprint: bool = False,
) -> tuple[C, FeatureArtifact]:
    found = await find_latest(uow, state, artifact_type, cls, current_blueprint=current_blueprint)
    if found is None:
        scope = f" del blueprint v{state.blueprint_version}" if current_blueprint else ""
        raise ContextAssemblyError(f"falta el artefacto {artifact_type.value}{scope}")
    return found


class Agent(ABC, Generic[T]):
    role: AgentRole
    node: str
    phase: Phase
    output_model: type[T]

    def __init__(self, deps: AgentDeps) -> None:
        self._deps = deps

    @property
    def artifact_type(self) -> ArtifactType:
        return artifact_type_for(self.output_model)

    @abstractmethod
    async def prepare(self, uow: UnitOfWork, state: FeatureDeliveryState) -> PreparedTask[T]:
        """Carga lo necesario de la BD y arma la tarea y el validador determinista."""

    @abstractmethod
    def outcome(self, output: T, state: FeatureDeliveryState) -> AgentOutcome:
        """Traduce la salida validada en el evento (o pausa) que propone el agente."""

    async def run(self, state: FeatureDeliveryState) -> AgentResult[T]:
        async with unit_of_work(self._deps.db) as uow:
            bundle = await self._deps.context.build(uow, state, self.node)
            prepared = await self.prepare(uow, state)
        routed = await self._deps.router.invoke(
            state=state,
            prompt=prompt_for(self.role),
            bundle=bundle,
            task=prepared.task,
            output_model=self.output_model,
            validator=prepared.validator,
        )
        return AgentResult(output=routed.output, routed=routed, bundle=bundle)
