"""Perfiles de negocio (giro por tenant) y memoria semántica (Context Packs + pgvector)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import Integer, and_, cast, false, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from specforge.core.errors import NotFoundError
from specforge.db.models import BusinessProfile, ContextChunk, ContextPack
from specforge.db.repositories._common import text_hash

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from specforge.core.enums import ChunkSourceKind


class BusinessProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(
        self,
        *,
        tenant_id: str,
        code: str,
        display_name: str,
        industry: str,
        config: Mapping[str, Any],
        is_active: bool = True,
    ) -> BusinessProfile:
        """Crea o actualiza; `config_version` sube solo si la configuración cambió."""
        insert_stmt = pg_insert(BusinessProfile).values(
            tenant_id=tenant_id,
            code=code,
            display_name=display_name,
            industry=industry,
            config=dict(config),
            is_active=is_active,
        )
        excluded = insert_stmt.excluded
        stmt = (
            insert_stmt.on_conflict_do_update(
                index_elements=["tenant_id", "code"],
                set_={
                    "display_name": excluded.display_name,
                    "industry": excluded.industry,
                    "config": excluded.config,
                    "is_active": excluded.is_active,
                    "config_version": BusinessProfile.config_version
                    + cast(BusinessProfile.config != excluded.config, Integer),
                },
            )
            .returning(BusinessProfile)
            .execution_options(populate_existing=True)
        )
        return (await self._s.scalars(stmt)).one()

    async def get(self, tenant_id: str, code: str) -> BusinessProfile:
        profile = await self._s.get(BusinessProfile, (tenant_id, code), populate_existing=True)
        if profile is None:
            raise NotFoundError(f"perfil de negocio no encontrado: {tenant_id}/{code}")
        return profile


@dataclass(frozen=True, slots=True)
class ChunkInput:
    source_kind: ChunkSourceKind
    module: str
    chunk_index: int
    content: str
    token_count: int = 0
    pack_code: str | None = None
    pack_version: int | None = None
    artifact_id: uuid.UUID | None = None
    feature_id: str | None = None
    tenant_id: str | None = None
    industry: str | None = None
    embedding: Sequence[float] | None = None
    embedding_model: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChunkHit:
    chunk: ContextChunk
    distance: float


class ContextRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def publish_pack(
        self,
        *,
        pack_code: str,
        module: str,
        title: str,
        content: str,
        industry: str | None = None,
        token_estimate: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextPack:
        """Publica una nueva versión activa. Si el contenido no cambió, devuelve la activa."""
        digest = text_hash(content)
        await self._s.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"context_pack:{pack_code}"}
        )
        active = await self.active_pack(pack_code)
        if active is not None and active.content_hash == digest:
            return active
        last_version = await self._s.scalar(
            select(func.coalesce(func.max(ContextPack.version), 0)).where(
                ContextPack.pack_code == pack_code
            )
        )
        if active is not None:
            await self._s.execute(
                update(ContextPack)
                .where(ContextPack.pack_code == pack_code, ContextPack.is_active.is_(True))
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
        stmt = (
            pg_insert(ContextPack)
            .values(
                pack_code=pack_code,
                version=int(last_version or 0) + 1,
                module=module,
                industry=industry,
                title=title,
                content=content,
                content_hash=digest,
                token_estimate=token_estimate,
                is_active=True,
                metadata_=dict(metadata or {}),
            )
            .returning(ContextPack)
        )
        return (await self._s.scalars(stmt)).one()

    async def active_pack(self, pack_code: str) -> ContextPack | None:
        pack: ContextPack | None = await self._s.scalar(
            select(ContextPack)
            .where(ContextPack.pack_code == pack_code, ContextPack.is_active.is_(True))
            .execution_options(populate_existing=True)
        )
        return pack

    async def active_packs_for(
        self, module: str, *, industry: str | None = None
    ) -> list[ContextPack]:
        """Packs activos de un módulo: los agnósticos + el overlay del giro indicado."""
        industry_filter = (
            or_(ContextPack.industry.is_(None), ContextPack.industry == industry)
            if industry is not None
            else ContextPack.industry.is_(None)
        )
        stmt = (
            select(ContextPack)
            .where(ContextPack.module == module, ContextPack.is_active.is_(True), industry_filter)
            .order_by(ContextPack.industry.nulls_first(), ContextPack.pack_code)
        )
        return list((await self._s.scalars(stmt)).all())

    async def count_chunks(self, pack_code: str, pack_version: int) -> int:
        count = await self._s.scalar(
            select(func.count()).where(
                ContextChunk.pack_code == pack_code, ContextChunk.pack_version == pack_version
            )
        )
        return int(count or 0)

    async def add_chunks(self, chunks: Sequence[ChunkInput]) -> list[uuid.UUID]:
        if not chunks:
            return []
        rows = [
            {
                "source_kind": c.source_kind.value,
                "module": c.module,
                "chunk_index": c.chunk_index,
                "content": c.content,
                "token_count": c.token_count,
                "pack_code": c.pack_code,
                "pack_version": c.pack_version,
                "artifact_id": c.artifact_id,
                "feature_id": c.feature_id,
                "tenant_id": c.tenant_id,
                "industry": c.industry,
                "embedding": list(c.embedding) if c.embedding is not None else None,
                "embedding_model": c.embedding_model,
                "metadata_": dict(c.metadata),
            }
            for c in chunks
        ]
        stmt = pg_insert(ContextChunk).values(rows).returning(ContextChunk.chunk_id)
        return list((await self._s.scalars(stmt)).all())

    async def search(
        self,
        embedding: Sequence[float],
        *,
        module: str,
        tenant_id: str | None = None,
        industry: str | None = None,
        source_kinds: Sequence[ChunkSourceKind] | None = None,
        k: int = 8,
    ) -> list[ChunkHit]:
        """RAG acotado: mismo módulo, conocimiento global o del tenant/giro, packs activos."""
        distance = ContextChunk.embedding.cosine_distance(list(embedding)).label("distance")
        tenant_scope = (
            or_(ContextChunk.tenant_id.is_(None), ContextChunk.tenant_id == tenant_id)
            if tenant_id is not None
            else ContextChunk.tenant_id.is_(None)
        )
        industry_scope = (
            or_(ContextChunk.industry.is_(None), ContextChunk.industry == industry)
            if industry is not None
            else ContextChunk.industry.is_(None)
        )
        stmt = (
            select(ContextChunk, distance)
            .outerjoin(
                ContextPack,
                and_(
                    ContextPack.pack_code == ContextChunk.pack_code,
                    ContextPack.version == ContextChunk.pack_version,
                ),
            )
            .where(
                ContextChunk.module == module,
                ContextChunk.embedding.is_not(None),
                or_(ContextChunk.pack_code.is_(None), ContextPack.is_active.is_(True)),
                tenant_scope,
                industry_scope,
            )
            .order_by(distance)
            .limit(k)
        )
        if source_kinds is not None:
            values = [s.value for s in source_kinds]
            stmt = stmt.where(ContextChunk.source_kind.in_(values) if values else false())
        rows = (await self._s.execute(stmt)).all()
        return [ChunkHit(chunk=row[0], distance=float(row[1])) for row in rows]
