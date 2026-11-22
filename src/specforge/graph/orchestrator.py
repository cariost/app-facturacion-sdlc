"""`SpecForgeOrchestrator`: conduce una feature por el grafo, hilo por hilo.

Cada hilo de LangGraph es `sf:<feature_id>:v<bp>:<run>`. Un hilo termina cuando la feature
cambia de versión de blueprint (cambios pedidos en HUMAN_GATE) o de ejecución (reanudación
de BLOCKED); el orquestador entonces abre el hilo nuevo con el estado leído de PostgreSQL.
Las pausas humanas (`interrupt`) se reanudan con `Command(resume=...)` en el mismo hilo.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from specforge.agents import AgentDeps, AgentSuite
from specforge.agents.base import find_latest
from specforge.core.enums import ApprovalGate, ArtifactType, Phase
from specforge.core.errors import GateNotPendingError, InvalidPhaseTransitionError
from specforge.core.transition_guard import TransitionEvent, TransitionGuard
from specforge.db.checkpointer import thread_config
from specforge.db.uow import unit_of_work
from specforge.graph.builder import compile_graph
from specforge.graph.nodes import STOP_PHASES, GraphNodes, merge_feature
from specforge.graph.state import (
    ClarificationResume,
    GateResume,
    GraphState,
    InterruptPayload,
    encode,
)
from specforge.schemas.delivery import QAReport, Verdict
from specforge.schemas.state import FeatureDeliveryState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langgraph.checkpoint.base import BaseCheckpointSaver

    from specforge.context.builder import ContextBuilder
    from specforge.context.embeddings import Embedder
    from specforge.db.engine import Database
    from specforge.db.models import Feature
    from specforge.db.uow import UnitOfWork
    from specforge.llm.router import ModelRouter

RECURSION_LIMIT = 80
MAX_THREAD_HOPS = 8


class RunStatus(StrEnum):
    PAUSED = "PAUSED"  # esperando una decisión humana
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    IDLE = "IDLE"  # sin ejecución en curso (p. ej. creada sin iniciar)


@dataclass(frozen=True, slots=True)
class RunResult:
    feature_id: str
    phase: Phase
    thread_id: str
    status: RunStatus
    interrupt: InterruptPayload | None = None


class SpecForgeOrchestrator:
    def __init__(
        self,
        *,
        db: Database,
        checkpointer: BaseCheckpointSaver[str],
        agents: AgentSuite,
        context: ContextBuilder,
        embedder: Embedder | None = None,
        guard: TransitionGuard | None = None,
    ) -> None:
        self._db = db
        self._context = context
        self._guard = guard or TransitionGuard()
        nodes = GraphNodes(
            db=db, agents=agents, context=context, guard=self._guard, embedder=embedder
        )
        self._graph = compile_graph(nodes, checkpointer)
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def build(
        cls,
        *,
        db: Database,
        checkpointer: BaseCheckpointSaver[str],
        context: ContextBuilder,
        router: ModelRouter,
        embedder: Embedder | None = None,
    ) -> SpecForgeOrchestrator:
        agents = AgentSuite.build(AgentDeps(db=db, context=context, router=router))
        return cls(
            db=db, checkpointer=checkpointer, agents=agents, context=context, embedder=embedder
        )

    @property
    def db(self) -> Database:
        return self._db

    # ------------------------------------------------------------- comandos

    async def create_feature(
        self,
        *,
        module: str,
        tenant_id: str,
        title: str,
        request: str,
        business_profile: str | None = None,
        max_retries: int = 3,
        active_constraints: Sequence[str] = (),
    ) -> Feature:
        async with unit_of_work(self._db) as uow:
            return await uow.features.create(
                module=module,
                tenant_id=tenant_id,
                title=title,
                request=request,
                business_profile=business_profile,
                max_retries=max_retries,
                active_constraints=active_constraints,
            )

    async def run(self, feature_id: str) -> RunResult:
        """Avanza la feature hasta la próxima pausa humana o un estado final."""
        async with self._lock(feature_id):
            return await self._drive(feature_id)

    async def decide_gate(self, feature_id: str, resume: GateResume) -> RunResult:
        async with self._lock(feature_id):
            await self._expect_pending(feature_id, {ApprovalGate.BLUEPRINT_APPROVAL})
            return await self._drive(feature_id, Command(resume=resume.model_dump(mode="json")))

    async def answer_clarification(self, feature_id: str, resume: ClarificationResume) -> RunResult:
        async with self._lock(feature_id):
            await self._expect_pending(
                feature_id,
                {ApprovalGate.INTAKE_CLARIFICATION, ApprovalGate.STORY_CLARIFICATION},
            )
            return await self._drive(feature_id, Command(resume=resume.model_dump(mode="json")))

    async def prepare_resume(self, feature_id: str, *, reviewer: str, reason: str) -> Feature:
        """Saca la feature de BLOCKED (nueva ejecución → nuevo hilo) sin correr el grafo."""
        async with self._lock(feature_id):
            if await self.pending_interrupt(feature_id) is not None:
                raise GateNotPendingError("hay una pausa humana pendiente; respóndela primero")
            async with unit_of_work(self._db) as uow:
                feature = await uow.features.get(feature_id)
                if Phase(feature.phase).is_terminal:
                    raise InvalidPhaseTransitionError(f"{feature_id} está {feature.phase}")
                if feature.phase != Phase.BLOCKED.value:
                    return feature
                state = FeatureDeliveryState.from_feature(feature)
                decision = self._guard.decide(
                    state, TransitionEvent.RESUME, reason=f"reanudada por {reviewer}: {reason}"
                )
                feature = await uow.features.apply(
                    feature_id, decision, expected_row_version=feature.row_version
                )
                feature = await uow.features.start_new_run(
                    feature_id, expected_row_version=feature.row_version
                )
                state = merge_feature(state, feature)
                await self._context.record_progress(
                    uow, state, [f"Reanudada por {reviewer}: {reason}"]
                )
                return await uow.features.get(feature_id)

    async def resume(self, feature_id: str, *, reviewer: str, reason: str) -> RunResult:
        await self.prepare_resume(feature_id, reviewer=reviewer, reason=reason)
        return await self.run(feature_id)

    # -------------------------------------------------------------- consultas

    async def pending_interrupt(self, feature_id: str) -> InterruptPayload | None:
        async with unit_of_work(self._db) as uow:
            feature = await uow.features.get(feature_id)
        snapshot = await self._graph.aget_state(self._config(feature.current_thread_id))
        if not snapshot.interrupts:
            return None
        return InterruptPayload.model_validate(snapshot.interrupts[0].value)

    async def status(self, feature_id: str) -> RunResult:
        async with unit_of_work(self._db) as uow:
            feature = await uow.features.get(feature_id)
        return self._result(feature, await self.pending_interrupt(feature_id))

    # ---------------------------------------------------------------- interno

    def _lock(self, feature_id: str) -> asyncio.Lock:
        return self._locks.setdefault(feature_id, asyncio.Lock())

    @staticmethod
    def _config(thread_id: str) -> Any:
        return {**thread_config(thread_id), "recursion_limit": RECURSION_LIMIT}

    async def _expect_pending(self, feature_id: str, gates: set[ApprovalGate]) -> None:
        pending = await self.pending_interrupt(feature_id)
        if pending is None or pending.gate not in gates:
            expected = ", ".join(sorted(g.value for g in gates))
            found = pending.gate.value if pending else "ninguna"
            raise GateNotPendingError(f"se esperaba una pausa {expected}; pendiente: {found}")

    async def _drive(self, feature_id: str, command: Command[Any] | None = None) -> RunResult:
        feature: Feature | None = None
        for _ in range(MAX_THREAD_HOPS):
            async with unit_of_work(self._db) as uow:
                feature = await uow.features.get(feature_id)
                config = self._config(feature.current_thread_id)
                snapshot = await self._graph.aget_state(config)
                graph_input: GraphState | Command[Any] | None
                if command is not None:
                    graph_input, command = command, None
                elif snapshot.interrupts:
                    break
                elif snapshot.next:
                    graph_input = None  # continúa una ejecución que se cortó
                elif snapshot.values:
                    break  # el hilo vigente ya terminó
                elif Phase(feature.phase) in STOP_PHASES:
                    break
                else:
                    graph_input = await self._initial_input(uow, feature)
            await self._graph.ainvoke(graph_input, config)
        assert feature is not None
        async with unit_of_work(self._db) as uow:
            feature = await uow.features.get(feature_id)
        return self._result(feature, await self.pending_interrupt(feature_id))

    async def _initial_input(self, uow: UnitOfWork, feature: Feature) -> GraphState:
        industry = None
        if feature.business_profile:
            profile = await uow.profiles.get(feature.tenant_id, feature.business_profile)
            industry = profile.industry
        state = FeatureDeliveryState.from_feature(feature, industry=industry)
        if state.phase is Phase.IMPLEMENTATION and state.retry_count > 0:
            # Reanudación de un reintento: se recupera la memoria episódica del último QA.
            report = await find_latest(
                uow, state, ArtifactType.QA_REPORT, QAReport, current_blueprint=True
            )
            if report is not None and report[0].verdict is Verdict.FAIL:
                state = state.model_copy(update={"qa_errors": report[0].to_qa_errors()})
        return {"run_thread": feature.current_thread_id, "delivery": encode(state)}

    @staticmethod
    def _result(feature: Feature, pending: InterruptPayload | None) -> RunResult:
        phase = Phase(feature.phase)
        if pending is not None:
            status = RunStatus.PAUSED
        elif phase is Phase.COMPLETED:
            status = RunStatus.COMPLETED
        elif phase is Phase.FAILED:
            status = RunStatus.FAILED
        elif phase is Phase.BLOCKED:
            status = RunStatus.BLOCKED
        else:
            status = RunStatus.IDLE
        return RunResult(
            feature_id=feature.feature_id,
            phase=phase,
            thread_id=feature.current_thread_id,
            status=status,
            interrupt=pending,
        )
