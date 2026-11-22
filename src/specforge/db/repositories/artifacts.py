"""Repositorios de artefactos versionados y aprobaciones HITL (ambos append-only)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from specforge.core.errors import IdempotencyConflictError, NotFoundError
from specforge.db.models import FeatureApproval, FeatureArtifact
from specforge.db.repositories._common import content_hash

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from specforge.core.enums import (
        AgentRole,
        ApprovalDecision,
        ApprovalGate,
        ArtifactType,
        Phase,
    )


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        feature_id: str,
        artifact_type: ArtifactType,
        content: Mapping[str, Any],
        produced_by: AgentRole,
        schema_name: str,
        phase: Phase,
        blueprint_version: int,
        thread_id: str,
        schema_version: str = "1",
        summary: str = "",
        execution_log_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> FeatureArtifact:
        """Agrega una nueva versión (la asigna la BD). Idempotente por `idempotency_key`."""
        digest = content_hash(content)
        stmt = pg_insert(FeatureArtifact).values(
            feature_id=feature_id,
            artifact_type=artifact_type.value,
            content=dict(content),
            content_hash=digest,
            produced_by=produced_by.value,
            schema_name=schema_name,
            schema_version=schema_version,
            phase=phase.value,
            blueprint_version=blueprint_version,
            thread_id=thread_id,
            summary=summary,
            execution_log_id=execution_log_id,
            idempotency_key=idempotency_key,
        )
        if idempotency_key is not None:
            stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])
        created = (await self._s.scalars(stmt.returning(FeatureArtifact))).one_or_none()
        if created is not None:
            return created

        # Solo ON CONFLICT DO NOTHING devuelve vacío: hubo una escritura previa.
        existing = await self._s.scalar(
            select(FeatureArtifact).where(FeatureArtifact.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise NotFoundError(f"idempotency_key sin artefacto: {idempotency_key}")
        if (
            existing.feature_id != feature_id
            or existing.artifact_type != artifact_type.value
            or existing.content_hash != digest
        ):
            raise IdempotencyConflictError(
                f"idempotency_key {idempotency_key!r} reutilizada con contenido distinto"
            )
        return existing

    async def get(self, artifact_id: uuid.UUID) -> FeatureArtifact:
        artifact = await self._s.get(FeatureArtifact, artifact_id)
        if artifact is None:
            raise NotFoundError(f"artefacto no encontrado: {artifact_id}")
        return artifact

    async def get_many(self, artifact_ids: Sequence[uuid.UUID]) -> list[FeatureArtifact]:
        """Resuelve los punteros (`artifact_ids`) del State en el orden recibido."""
        if not artifact_ids:
            return []
        stmt = select(FeatureArtifact).where(FeatureArtifact.artifact_id.in_(artifact_ids))
        by_id = {a.artifact_id: a for a in (await self._s.scalars(stmt)).all()}
        missing = [str(i) for i in artifact_ids if i not in by_id]
        if missing:
            raise NotFoundError(f"artefactos no encontrados: {', '.join(missing)}")
        return [by_id[i] for i in artifact_ids]

    async def latest(
        self,
        feature_id: str,
        artifact_type: ArtifactType,
        *,
        blueprint_version: int | None = None,
    ) -> FeatureArtifact | None:
        stmt = (
            select(FeatureArtifact)
            .where(
                FeatureArtifact.feature_id == feature_id,
                FeatureArtifact.artifact_type == artifact_type.value,
            )
            .order_by(FeatureArtifact.version.desc())
            .limit(1)
        )
        if blueprint_version is not None:
            stmt = stmt.where(FeatureArtifact.blueprint_version == blueprint_version)
        artifact: FeatureArtifact | None = await self._s.scalar(stmt)
        return artifact

    async def list_for_feature(
        self, feature_id: str, *, artifact_type: ArtifactType | None = None
    ) -> list[FeatureArtifact]:
        stmt = (
            select(FeatureArtifact)
            .where(FeatureArtifact.feature_id == feature_id)
            .order_by(FeatureArtifact.created_at, FeatureArtifact.version)
        )
        if artifact_type is not None:
            stmt = stmt.where(FeatureArtifact.artifact_type == artifact_type.value)
        return list((await self._s.scalars(stmt)).all())


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        *,
        feature_id: str,
        gate: ApprovalGate,
        decision: ApprovalDecision,
        phase: Phase,
        blueprint_version: int,
        reviewer: str,
        thread_id: str,
        artifact_id: uuid.UUID | None = None,
        comments: str = "",
        payload: Mapping[str, Any] | None = None,
        interrupt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> FeatureApproval:
        payload_dict = dict(payload or {})
        stmt = pg_insert(FeatureApproval).values(
            feature_id=feature_id,
            gate=gate.value,
            decision=decision.value,
            phase=phase.value,
            blueprint_version=blueprint_version,
            artifact_id=artifact_id,
            reviewer=reviewer,
            comments=comments,
            payload=payload_dict,
            thread_id=thread_id,
            interrupt_id=interrupt_id,
            idempotency_key=idempotency_key,
        )
        if idempotency_key is not None:
            stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])
        created = (await self._s.scalars(stmt.returning(FeatureApproval))).one_or_none()
        if created is not None:
            return created

        existing = await self._s.scalar(
            select(FeatureApproval).where(FeatureApproval.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise NotFoundError(f"idempotency_key sin aprobación: {idempotency_key}")
        same = (
            existing.feature_id == feature_id
            and existing.gate == gate.value
            and existing.decision == decision.value
            and existing.comments == comments
            and content_hash(existing.payload) == content_hash(payload_dict)
        )
        if not same:
            raise IdempotencyConflictError(
                f"idempotency_key {idempotency_key!r} reutilizada con otra decisión"
            )
        return existing

    async def list_for_feature(self, feature_id: str) -> list[FeatureApproval]:
        stmt = (
            select(FeatureApproval)
            .where(FeatureApproval.feature_id == feature_id)
            .order_by(FeatureApproval.decided_at)
        )
        return list((await self._s.scalars(stmt)).all())

    async def latest_for_gate(
        self, feature_id: str, gate: ApprovalGate, *, blueprint_version: int | None = None
    ) -> FeatureApproval | None:
        stmt = (
            select(FeatureApproval)
            .where(FeatureApproval.feature_id == feature_id, FeatureApproval.gate == gate.value)
            .order_by(FeatureApproval.decided_at.desc())
            .limit(1)
        )
        if blueprint_version is not None:
            stmt = stmt.where(FeatureApproval.blueprint_version == blueprint_version)
        approval: FeatureApproval | None = await self._s.scalar(stmt)
        return approval
