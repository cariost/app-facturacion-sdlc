"""API FastAPI de SpecForge.

    POST /features                              crea (y opcionalmente inicia) una feature
    GET  /features/{id}                         estado, pausa pendiente, artefactos, consumo
    GET  /features/{id}/artifacts               artefactos (filtrables por tipo)
    GET  /features/{id}/artifacts/{artifact_id} contenido de un artefacto
    GET  /features/{id}/history                 historial de fases
    POST /features/{id}/approval                HUMAN_GATE: aprobar, pedir cambios o rechazar
    POST /features/{id}/clarification           responder preguntas críticas
    POST /features/{id}/resume                  reanudar una feature BLOCKED o cortada

Las acciones que corren el grafo responden 202 y ejecutan en segundo plano; el progreso se
consulta con GET. Arranque: `uvicorn specforge.api.app:create_app --factory`.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from specforge.api.schemas import (
    ActionAccepted,
    ApprovalView,
    ArtifactDetail,
    ArtifactSummary,
    CreateFeatureRequest,
    FeatureView,
    PhaseHistoryItem,
    ResumeRequest,
    UsageView,
)
from specforge.core.enums import ApprovalGate, ArtifactType
from specforge.core.errors import (
    ConcurrencyConflictError,
    ContractViolationError,
    GateNotPendingError,
    IdempotencyConflictError,
    ImmutableRecordError,
    InvalidPhaseTransitionError,
    NotFoundError,
    SpecForgeError,
)
from specforge.db.uow import unit_of_work
from specforge.graph.orchestrator import SpecForgeOrchestrator
from specforge.graph.state import ClarificationResume, GateResume

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from specforge.core.config import Settings
    from specforge.db.models import FeatureArtifact
    from specforge.graph.orchestrator import RunResult

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR: tuple[tuple[type[SpecForgeError], int], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (GateNotPendingError, status.HTTP_409_CONFLICT),
    (ConcurrencyConflictError, status.HTTP_409_CONFLICT),
    (InvalidPhaseTransitionError, status.HTTP_409_CONFLICT),
    (IdempotencyConflictError, status.HTTP_409_CONFLICT),
    (ImmutableRecordError, status.HTTP_409_CONFLICT),
    (ContractViolationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
)


def get_orchestrator(request: Request) -> SpecForgeOrchestrator:
    orchestrator: SpecForgeOrchestrator = request.app.state.orchestrator
    return orchestrator


Orchestrator = Annotated[SpecForgeOrchestrator, Depends(get_orchestrator)]


async def _run_safely(action: Callable[[], Awaitable[RunResult]], feature_id: str) -> None:
    try:
        result = await action()
        logger.info("feature %s → %s (%s)", feature_id, result.phase, result.status)
    except Exception:
        logger.exception("falló la ejecución de %s", feature_id)


def _artifact_summary(a: FeatureArtifact) -> ArtifactSummary:
    return ArtifactSummary(
        artifact_id=a.artifact_id,
        artifact_type=a.artifact_type,
        version=a.version,
        blueprint_version=a.blueprint_version,
        phase=a.phase,
        produced_by=a.produced_by,
        schema_name=a.schema_name,
        summary=a.summary,
        supersedes_artifact_id=a.supersedes_artifact_id,
        execution_log_id=a.execution_log_id,
        thread_id=a.thread_id,
        created_at=a.created_at,
    )


async def feature_view(orchestrator: SpecForgeOrchestrator, feature_id: str) -> FeatureView:
    result = await orchestrator.status(feature_id)
    async with unit_of_work(orchestrator.db) as uow:
        f = await uow.features.get(feature_id)
        artifacts = await uow.artifacts.list_for_feature(feature_id)
        approvals = await uow.approvals.list_for_feature(feature_id)
        usage = await uow.logs.usage_by_tier(feature_id)
    return FeatureView(
        feature_id=f.feature_id,
        tenant_id=f.tenant_id,
        module=f.module,
        business_profile=f.business_profile,
        title=f.title,
        phase=f.phase,
        status=result.status.value,
        blocked_from_phase=f.blocked_from_phase,
        blocked_reason=f.blocked_reason,
        last_transition_reason=f.last_transition_reason,
        blueprint_version=f.blueprint_version,
        run_seq=f.run_seq,
        thread_id=f.current_thread_id,
        retry_count=f.retry_count,
        max_retries=f.max_retries,
        executive_summary=f.executive_summary,
        active_constraints=list(f.active_constraints),
        created_at=f.created_at,
        updated_at=f.updated_at,
        closed_at=f.closed_at,
        pending_interrupt=result.interrupt,
        artifacts=[_artifact_summary(a) for a in artifacts],
        approvals=[
            ApprovalView(
                approval_id=a.approval_id,
                gate=a.gate,
                decision=a.decision,
                blueprint_version=a.blueprint_version,
                reviewer=a.reviewer,
                comments=a.comments,
                payload=a.payload,
                thread_id=a.thread_id,
                decided_at=a.decided_at,
            )
            for a in approvals
        ],
        usage=[
            UsageView(
                model_tier=u.model_tier,
                calls=u.calls,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cached_tokens=u.cached_tokens,
                latency_ms=u.latency_ms,
            )
            for u in usage
        ],
    )


def _default_lifespan(
    settings: Settings | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from specforge.context import ContextBuilder, HashingEmbedder
        from specforge.core.config import get_settings
        from specforge.db import Database
        from specforge.db.checkpointer import open_checkpointer
        from specforge.graph.orchestrator import SpecForgeOrchestrator
        from specforge.llm import ModelRouter, RoutingPolicy
        from specforge.llm.anthropic_client import AnthropicLLMClient

        cfg = settings or get_settings()
        db = Database(cfg)
        try:
            async with open_checkpointer(cfg) as saver:
                embedder = HashingEmbedder()
                context = ContextBuilder(embedder=embedder)
                router = ModelRouter(
                    db=db,
                    client=AnthropicLLMClient(refusal_fallback=cfg.llm_refusal_fallback),
                    policy=RoutingPolicy.from_settings(cfg),
                    max_repairs=cfg.llm_max_repairs,
                )
                app.state.orchestrator = SpecForgeOrchestrator.build(
                    db=db, checkpointer=saver, context=context, router=router, embedder=embedder
                )
                yield
        finally:
            await db.dispose()

    return lifespan


def create_app(
    *, orchestrator: SpecForgeOrchestrator | None = None, settings: Settings | None = None
) -> FastAPI:
    # Con un orquestador inyectado (tests, embebido en otro proceso) no hay lifespan propio.
    lifespan = None if orchestrator is not None else _default_lifespan(settings)
    app = FastAPI(title="SpecForge", version="0.1.0", lifespan=lifespan)
    if orchestrator is not None:
        app.state.orchestrator = orchestrator

    @app.exception_handler(SpecForgeError)
    async def domain_error(_: Request, exc: SpecForgeError) -> JSONResponse:
        code = next(
            (s for cls, s in _STATUS_BY_ERROR if isinstance(exc, cls)),
            status.HTTP_400_BAD_REQUEST,
        )
        body: dict[str, object] = {"error": type(exc).__name__, "detail": str(exc)}
        if isinstance(exc, ContractViolationError):
            body["violations"] = exc.violations
        return JSONResponse(status_code=code, content=body)

    @app.post("/features", status_code=status.HTTP_201_CREATED, response_model=FeatureView)
    async def create_feature(
        body: CreateFeatureRequest, orchestrator: Orchestrator, background: BackgroundTasks
    ) -> FeatureView:
        feature = await orchestrator.create_feature(
            module=body.module,
            tenant_id=body.tenant_id,
            title=body.title,
            request=body.request,
            business_profile=body.business_profile,
            max_retries=body.max_retries,
            active_constraints=body.active_constraints,
        )
        if body.start:
            fid = feature.feature_id
            background.add_task(_run_safely, lambda: orchestrator.run(fid), fid)
        return await feature_view(orchestrator, feature.feature_id)

    @app.get("/features/{feature_id}", response_model=FeatureView)
    async def get_feature(feature_id: str, orchestrator: Orchestrator) -> FeatureView:
        return await feature_view(orchestrator, feature_id)

    @app.get("/features/{feature_id}/artifacts", response_model=list[ArtifactSummary])
    async def list_artifacts(
        feature_id: str, orchestrator: Orchestrator, artifact_type: ArtifactType | None = None
    ) -> list[ArtifactSummary]:
        async with unit_of_work(orchestrator.db) as uow:
            await uow.features.get(feature_id)
            items = await uow.artifacts.list_for_feature(feature_id, artifact_type=artifact_type)
        return [_artifact_summary(a) for a in items]

    @app.get("/features/{feature_id}/artifacts/{artifact_id}", response_model=ArtifactDetail)
    async def get_artifact(
        feature_id: str, artifact_id: uuid.UUID, orchestrator: Orchestrator
    ) -> ArtifactDetail:
        async with unit_of_work(orchestrator.db) as uow:
            artifact = await uow.artifacts.get(artifact_id)
        if artifact.feature_id != feature_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artefacto de otra feature")
        return ArtifactDetail(
            **_artifact_summary(artifact).model_dump(),
            content=artifact.content,
            content_hash=artifact.content_hash,
        )

    @app.get("/features/{feature_id}/history", response_model=list[PhaseHistoryItem])
    async def history(feature_id: str, orchestrator: Orchestrator) -> list[PhaseHistoryItem]:
        async with unit_of_work(orchestrator.db) as uow:
            await uow.features.get(feature_id)
            rows = await uow.features.phase_history(feature_id)
        return [
            PhaseHistoryItem(
                from_phase=r.from_phase,
                to_phase=r.to_phase,
                blueprint_version=r.blueprint_version,
                retry_count=r.retry_count,
                thread_id=r.thread_id,
                reason=r.reason,
                changed_at=r.changed_at,
            )
            for r in rows
        ]

    @app.post(
        "/features/{feature_id}/approval",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ActionAccepted,
    )
    async def approval(
        feature_id: str, body: GateResume, orchestrator: Orchestrator, background: BackgroundTasks
    ) -> ActionAccepted:
        pending = await orchestrator.pending_interrupt(feature_id)
        if pending is None or pending.gate is not ApprovalGate.BLUEPRINT_APPROVAL:
            raise GateNotPendingError(f"{feature_id} no espera aprobación del blueprint")
        background.add_task(
            _run_safely, lambda: orchestrator.decide_gate(feature_id, body), feature_id
        )
        return ActionAccepted(
            feature_id=feature_id, action=body.decision.value, thread_id=pending.thread_id
        )

    @app.post(
        "/features/{feature_id}/clarification",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ActionAccepted,
    )
    async def clarification(
        feature_id: str,
        body: ClarificationResume,
        orchestrator: Orchestrator,
        background: BackgroundTasks,
    ) -> ActionAccepted:
        pending = await orchestrator.pending_interrupt(feature_id)
        if pending is None or pending.gate is ApprovalGate.BLUEPRINT_APPROVAL:
            raise GateNotPendingError(f"{feature_id} no espera aclaraciones")
        unknown = sorted(set(body.answers) - {q.id for q in pending.questions})
        if unknown:
            raise ContractViolationError([f"pregunta desconocida: {q}" for q in unknown])
        background.add_task(
            _run_safely, lambda: orchestrator.answer_clarification(feature_id, body), feature_id
        )
        return ActionAccepted(
            feature_id=feature_id,
            action="REJECTED" if body.reject else "CLARIFIED",
            thread_id=pending.thread_id,
        )

    @app.post(
        "/features/{feature_id}/resume",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ActionAccepted,
    )
    async def resume(
        feature_id: str,
        body: ResumeRequest,
        orchestrator: Orchestrator,
        background: BackgroundTasks,
    ) -> ActionAccepted:
        feature = await orchestrator.prepare_resume(
            feature_id, reviewer=body.reviewer, reason=body.reason
        )
        background.add_task(_run_safely, lambda: orchestrator.run(feature_id), feature_id)
        return ActionAccepted(
            feature_id=feature_id, action="RESUME", thread_id=feature.current_thread_id
        )

    return app
