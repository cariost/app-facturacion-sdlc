"""Repositorios async sobre `AsyncSession`.

- No hacen commit: la transacción la controla `Database.session()` / `UnitOfWork`.
- Las reglas deterministas (grafo de fases, reintentos, versionado) las hace cumplir
  PostgreSQL; aquí solo se construyen sentencias y se traducen resultados.
- Escrituras idempotentes por `idempotency_key` (artefactos y aprobaciones).
"""

from specforge.db.repositories._common import content_hash, text_hash
from specforge.db.repositories.artifacts import ApprovalRepository, ArtifactRepository
from specforge.db.repositories.context import (
    BusinessProfileRepository,
    ChunkHit,
    ChunkInput,
    ContextRepository,
)
from specforge.db.repositories.execution_logs import ExecutionLogRepository, TokenUsage
from specforge.db.repositories.features import FeatureRepository

__all__ = [
    "ApprovalRepository",
    "ArtifactRepository",
    "BusinessProfileRepository",
    "ChunkHit",
    "ChunkInput",
    "ContextRepository",
    "ExecutionLogRepository",
    "FeatureRepository",
    "TokenUsage",
    "content_hash",
    "text_hash",
]
