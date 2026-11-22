"""Nodos del grafo.

Cada nodo es idempotente frente a re-ejecuciones de LangGraph (reanudación de un
`interrupt` o reintento tras una caída): primero relee la feature y, si la fase ya avanzó o
el hilo ya no es el vigente, no hace nada. Todo efecto de un nodo (artefacto, transición,
resumen ejecutivo) se escribe en una sola transacción. Las fallas de modelo o de contexto
bloquean la feature (BLOCKED) con un motivo en lugar de romper el grafo.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langgraph.graph import END
from langgraph.types import interrupt

from specforge.agents.base import find_latest, load_latest
from specforge.core.enums import (
    ApprovalDecision,
    ApprovalGate,
    ArtifactType,
    ChunkSourceKind,
    Phase,
)
from specforge.core.errors import ContextAssemblyError, LLMError
from specforge.core.transition_guard import (
    TransitionEvent,
    TransitionGuard,
    event_for_gate_decision,
)
from specforge.db.repositories import ChunkInput
from specforge.db.uow import unit_of_work
from specforge.graph.state import (
    GATE_DECISIONS,
    ClarificationResume,
    GateResume,
    GraphState,
    InterruptPayload,
    QuestionView,
    decode,
    encode,
)
from specforge.schemas.blueprint import ArchitectureBlueprint
from specforge.schemas.registry import serialize
from specforge.schemas.state import FeatureDeliveryState, PendingInterrupt
from specforge.schemas.stories import IntakeBrief, StoryRefinementOutput

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from specforge.agents import AgentSuite
    from specforge.agents.base import Agent
    from specforge.context.builder import ContextBuilder
    from specforge.context.embeddings import Embedder
    from specforge.db.engine import Database
    from specforge.db.models import Feature
    from specforge.db.uow import UnitOfWork

logger = logging.getLogger(__name__)

MAX_CLARIFICATIONS_PER_GATE = 3
MAX_REFINEMENT_ROUNDS = 3
MAX_REASON_CHARS = 300

PHASE_NODES: dict[Phase, str] = {
    Phase.INTAKE_PO: "intake_po",
    Phase.STORY_REFINEMENT: "story_refinement",
    Phase.BLUEPRINT_DESIGN: "blueprint_design",
    Phase.HUMAN_GATE: "human_gate",
    Phase.IMPLEMENTATION: "implementation",
    Phase.QA_VERIFICATION: "qa_verification",
}
STOP_PHASES = frozenset({Phase.COMPLETED, Phase.FAILED, Phase.BLOCKED})
ROUTES = [*PHASE_NODES.values(), "clarification", END]

Update = dict[str, Any]


def _clip(text: str, limit: int = MAX_REASON_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def merge_feature(state: FeatureDeliveryState, feature: Feature) -> FeatureDeliveryState:
    """State actualizado desde la fila de `features`, conservando la memoria de trabajo."""
    fresh = FeatureDeliveryState.from_feature(feature, industry=state.industry)
    same_blueprint = fresh.blueprint_version == state.blueprint_version
    return fresh.model_copy(
        update={
            "artifact_ids": dict(state.artifact_ids),
            "qa_errors": list(state.qa_errors) if same_blueprint else [],
            "pending_interrupt": state.pending_interrupt,
            "last_error": state.last_error,
        }
    )


class GraphNodes:
    def __init__(
        self,
        *,
        db: Database,
        agents: AgentSuite,
        context: ContextBuilder,
        guard: TransitionGuard | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._db = db
        self._agents = agents
        self._context = context
        self._guard = guard or TransitionGuard()
        self._embedder = embedder

    # ----------------------------------------------------------------- routing

    @staticmethod
    def route(graph_state: GraphState) -> str:
        state = decode(graph_state)
        if state.thread_id != graph_state["run_thread"]:
            return END  # nueva versión de blueprint o nueva ejecución: otro hilo
        if state.pending_interrupt is not None:
            return "clarification"
        if state.phase in STOP_PHASES:
            return END
        return PHASE_NODES[state.phase]

    def agent_node(self, phase: Phase) -> Callable[[GraphState], Awaitable[Update]]:
        agent = self._agents.for_phase(phase)

        async def node(graph_state: GraphState) -> Update:
            return await self._run_agent(agent, graph_state)

        node.__name__ = agent.node
        return node

    # ------------------------------------------------------------ agent nodes

    async def _refresh(self, uow: UnitOfWork, state: FeatureDeliveryState) -> FeatureDeliveryState:
        return merge_feature(state, await uow.features.get(state.feature_id))

    async def _run_agent(self, agent: Agent[Any], graph_state: GraphState) -> Update:
        state = decode(graph_state)
        async with unit_of_work(self._db) as uow:
            state = await self._refresh(uow, state)
        if state.phase is not agent.phase or state.thread_id != graph_state["run_thread"]:
            return {"delivery": encode(state)}

        try:
            result = await agent.run(state)
        except (LLMError, ContextAssemblyError) as exc:
            logger.warning("%s bloqueó %s: %s", agent.node, state.feature_id, exc)
            return {"delivery": encode(await self._block(state, f"{agent.node}: {exc}"))}

        outcome = agent.outcome(result.output, state)
        serialized = serialize(result.output)
        async with unit_of_work(self._db) as uow:
            artifact = await uow.artifacts.add(
                feature_id=state.feature_id,
                artifact_type=serialized.artifact_type,
                content=serialized.content,
                produced_by=agent.role,
                schema_name=serialized.schema_name,
                schema_version=serialized.schema_version,
                phase=state.phase,
                blueprint_version=state.blueprint_version,
                thread_id=state.thread_id,
                summary=_clip(outcome.summary),
                execution_log_id=result.routed.log_id,
            )
            state = state.model_copy(
                update=state.with_artifact(serialized.artifact_type, artifact.artifact_id)
            )
            if outcome.qa_errors is not None:
                state = state.model_copy(update={"qa_errors": outcome.qa_errors})
                if outcome.qa_errors:
                    await self._store_episode(uow, state, artifact.artifact_id, outcome.qa_errors)

            event, reason, pending = outcome.event, outcome.summary, None
            if outcome.clarification is not None:
                gate = outcome.clarification.gate
                asked = await self._count_approvals(uow, state, gate)
                if asked >= MAX_CLARIFICATIONS_PER_GATE:
                    event, reason = TransitionEvent.BLOCK, f"límite de aclaraciones en {gate}"
                else:
                    pending = PendingInterrupt(
                        interrupt_id=f"{state.thread_id}:{gate.value}:{asked + 1}",
                        gate=gate,
                        reason=outcome.clarification.reason,
                        question_ids=list(outcome.clarification.question_ids),
                        artifact_id=artifact.artifact_id,
                    )
            elif event is None:
                rounds = await self._count_artifacts(uow, state, serialized.artifact_type)
                if rounds >= MAX_REFINEMENT_ROUNDS:
                    event = TransitionEvent.BLOCK
                    reason = f"{rounds} rondas de refinamiento sin historias READY"

            state = state.model_copy(update={"pending_interrupt": pending, "last_error": None})
            state = await self._advance(uow, state, [outcome.summary], event, reason)
        return {"delivery": encode(state)}

    async def _advance(
        self,
        uow: UnitOfWork,
        state: FeatureDeliveryState,
        entries: list[str],
        event: TransitionEvent | None = None,
        reason: str = "",
    ) -> FeatureDeliveryState:
        """Registra eventos en el resumen ejecutivo y, si hay evento, aplica la transición.

        El resumen se escribe antes de transicionar (con la cabecera de la fase destino)
        porque una feature COMPLETED/FAILED queda inmutable.
        """
        if event is None:
            return await self._context.record_progress(uow, state, entries)
        decision = self._guard.decide(state, event, reason=_clip(reason))
        projected = state.model_copy(
            update=state.after_transition(decision, row_version=state.row_version)
        )
        state = await self._context.record_progress(uow, state, entries, header_state=projected)
        feature = await uow.features.apply(
            state.feature_id, decision, expected_row_version=state.row_version
        )
        return merge_feature(state, feature)

    async def _block(self, state: FeatureDeliveryState, reason: str) -> FeatureDeliveryState:
        async with unit_of_work(self._db) as uow:
            state = await self._refresh(uow, state)
            if state.phase in STOP_PHASES:
                return state
            state = state.model_copy(update={"last_error": _clip(reason)})
            return await self._advance(
                uow, state, [f"⚠ bloqueada: {reason}"], TransitionEvent.BLOCK, reason
            )

    async def _store_episode(
        self,
        uow: UnitOfWork,
        state: FeatureDeliveryState,
        artifact_id: Any,
        errors: list[str],
    ) -> None:
        """Memoria episódica: los errores de QA quedan recuperables por similitud."""
        content = f"Errores de QA en {state.feature_id} (intento {state.retry_count + 1}):\n"
        content += "\n".join(f"- {e}" for e in errors)
        vector = (await self._embedder.embed([content]))[0] if self._embedder else None
        await uow.context.add_chunks(
            [
                ChunkInput(
                    source_kind=ChunkSourceKind.EPISODIC,
                    module=state.module,
                    chunk_index=0,
                    content=content,
                    artifact_id=artifact_id,
                    feature_id=state.feature_id,
                    tenant_id=state.tenant_id,
                    industry=state.industry,
                    embedding=vector,
                    embedding_model=self._embedder.model_name if self._embedder else None,
                    metadata={"blueprint_version": state.blueprint_version},
                )
            ]
        )

    @staticmethod
    async def _count_approvals(
        uow: UnitOfWork, state: FeatureDeliveryState, gate: ApprovalGate
    ) -> int:
        approvals = await uow.approvals.list_for_feature(state.feature_id)
        return sum(
            1
            for a in approvals
            if a.gate == gate.value and a.blueprint_version == state.blueprint_version
        )

    @staticmethod
    async def _count_artifacts(
        uow: UnitOfWork, state: FeatureDeliveryState, artifact_type: ArtifactType
    ) -> int:
        artifacts = await uow.artifacts.list_for_feature(
            state.feature_id, artifact_type=artifact_type
        )
        return sum(1 for a in artifacts if a.blueprint_version == state.blueprint_version)

    # ---------------------------------------------------------- HITL nodes

    async def clarification(self, graph_state: GraphState) -> Update:
        state = decode(graph_state)
        pending = state.pending_interrupt
        if pending is None:
            return {}
        async with unit_of_work(self._db) as uow:
            questions = await self._questions(uow, state, pending)
        payload = InterruptPayload(
            interrupt_id=pending.interrupt_id,
            gate=pending.gate,
            feature_id=state.feature_id,
            thread_id=state.thread_id,
            phase=state.phase.value,
            blueprint_version=state.blueprint_version,
            artifact_id=str(pending.artifact_id) if pending.artifact_id else None,
            reason=pending.reason,
            summary=state.executive_summary,
            questions=questions,
            allowed_decisions=[ApprovalDecision.CLARIFIED, ApprovalDecision.REJECTED],
        )
        resume = ClarificationResume.model_validate(interrupt(payload.model_dump(mode="json")))

        decision = ApprovalDecision.REJECTED if resume.reject else ApprovalDecision.CLARIFIED
        async with unit_of_work(self._db) as uow:
            await uow.approvals.record(
                feature_id=state.feature_id,
                gate=pending.gate,
                decision=decision,
                phase=state.phase,
                blueprint_version=state.blueprint_version,
                reviewer=resume.reviewer,
                thread_id=state.thread_id,
                artifact_id=pending.artifact_id,
                comments=resume.comments,
                payload={"answers": resume.answers} if resume.answers else {},
                interrupt_id=pending.interrupt_id,
                idempotency_key=pending.interrupt_id,
            )
            state = (await self._refresh(uow, state)).model_copy(update={"pending_interrupt": None})
            answered = ", ".join(sorted(resume.answers)) or "-"
            line = f"Aclaración de {resume.reviewer}: {decision.value} (respuestas {answered})"
            reject = resume.reject and state.phase not in STOP_PHASES
            state = await self._advance(
                uow,
                state,
                [line],
                TransitionEvent.FAIL if reject else None,
                f"rechazada por {resume.reviewer}",
            )
        return {"delivery": encode(state)}

    @staticmethod
    async def _questions(
        uow: UnitOfWork, state: FeatureDeliveryState, pending: PendingInterrupt
    ) -> list[QuestionView]:
        source = (
            await find_latest(uow, state, ArtifactType.INTAKE_BRIEF, IntakeBrief)
            if pending.gate is ApprovalGate.INTAKE_CLARIFICATION
            else await find_latest(uow, state, ArtifactType.STORY_REFINEMENT, StoryRefinementOutput)
        )
        if source is None:
            return []
        wanted = set(pending.question_ids)
        return [
            QuestionView(
                id=q.id, question=q.question, severity=q.severity.value, story_ids=q.story_ids
            )
            for q in source[0].open_questions
            if q.id in wanted
        ]

    async def human_gate(self, graph_state: GraphState) -> Update:
        state = decode(graph_state)
        async with unit_of_work(self._db) as uow:
            state = await self._refresh(uow, state)
            if state.phase is not Phase.HUMAN_GATE or state.thread_id != graph_state["run_thread"]:
                return {"delivery": encode(state)}
            blueprint, artifact = await load_latest(
                uow,
                state,
                ArtifactType.ARCHITECTURE_BLUEPRINT,
                ArchitectureBlueprint,
                current_blueprint=True,
            )
        interrupt_id = f"{state.thread_id}:{ApprovalGate.BLUEPRINT_APPROVAL.value}"
        payload = InterruptPayload(
            interrupt_id=interrupt_id,
            gate=ApprovalGate.BLUEPRINT_APPROVAL,
            feature_id=state.feature_id,
            thread_id=state.thread_id,
            phase=state.phase.value,
            blueprint_version=state.blueprint_version,
            artifact_id=str(artifact.artifact_id),
            reason="aprobación del blueprint antes de implementar",
            summary=blueprint.summary,
            allowed_decisions=list(GATE_DECISIONS),
        )
        resume = GateResume.model_validate(interrupt(payload.model_dump(mode="json")))

        async with unit_of_work(self._db) as uow:
            await uow.approvals.record(
                feature_id=state.feature_id,
                gate=ApprovalGate.BLUEPRINT_APPROVAL,
                decision=resume.decision,
                phase=Phase.HUMAN_GATE,
                blueprint_version=state.blueprint_version,
                reviewer=resume.reviewer,
                thread_id=state.thread_id,
                artifact_id=artifact.artifact_id,
                comments=resume.comments,
                payload={**resume.payload, "target": resume.target.value},
                interrupt_id=interrupt_id,
                idempotency_key=interrupt_id,
            )
            state = await self._refresh(uow, state)
            if state.phase is Phase.HUMAN_GATE:
                reason = resume.comments or f"{resume.decision.value} por {resume.reviewer}"
                event = event_for_gate_decision(resume.decision, resume.target)
                line = (
                    f"Gate blueprint v{blueprint.blueprint_version}: {resume.decision.value}"
                    f" ({resume.reviewer})" + (f" — {resume.comments}" if resume.comments else "")
                )
                state = await self._advance(uow, state, [line], event, reason)
        return {"delivery": encode(state)}
