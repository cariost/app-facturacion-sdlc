"""Publicación idempotente de los Context Packs y de sus chunks en pgvector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from specforge.context_packs.loader import PackDefinition, load_seed_packs
from specforge.core.enums import ChunkSourceKind
from specforge.db.repositories import ChunkInput

if TYPE_CHECKING:
    from collections.abc import Sequence

    from specforge.context.embeddings import Embedder
    from specforge.db.uow import UnitOfWork


@dataclass(frozen=True, slots=True)
class SeedResult:
    pack_code: str
    version: int
    chunks_added: int

    @property
    def changed(self) -> bool:
        return self.chunks_added > 0


async def publish_definition(
    uow: UnitOfWork, pack: PackDefinition, *, embedder: Embedder | None = None
) -> SeedResult:
    """Publica el pack (nueva versión solo si cambió) y crea sus chunks si faltan."""
    stored = await uow.context.publish_pack(
        pack_code=pack.pack_code,
        module=pack.module,
        title=pack.title,
        content=pack.content,
        industry=pack.industry,
        token_estimate=pack.token_estimate,
        metadata={"source": pack.source} if pack.source else None,
    )
    if await uow.context.count_chunks(stored.pack_code, stored.version):
        return SeedResult(stored.pack_code, stored.version, 0)
    chunks = pack.chunks()
    vectors: list[list[float]] | list[None] = (
        await embedder.embed([c.content for c in chunks]) if embedder else [None] * len(chunks)
    )
    inputs = [
        ChunkInput(
            source_kind=ChunkSourceKind.CONTEXT_PACK,
            module=pack.module,
            chunk_index=chunk.index,
            content=chunk.content,
            token_count=chunk.token_count,
            pack_code=stored.pack_code,
            pack_version=stored.version,
            industry=pack.industry,
            embedding=vector,
            embedding_model=embedder.model_name if embedder and vector is not None else None,
            metadata={"heading": chunk.heading},
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    await uow.context.add_chunks(inputs)
    return SeedResult(stored.pack_code, stored.version, len(inputs))


async def seed_context_packs(
    uow: UnitOfWork,
    *,
    embedder: Embedder | None = None,
    packs: Sequence[PackDefinition] | None = None,
) -> list[SeedResult]:
    definitions = list(packs) if packs is not None else load_seed_packs()
    return [await publish_definition(uow, pack, embedder=embedder) for pack in definitions]
