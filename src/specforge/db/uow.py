"""Unit of Work: una transacción, todos los repositorios."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from specforge.db.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    BusinessProfileRepository,
    ContextRepository,
    ExecutionLogRepository,
    FeatureRepository,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from specforge.db.engine import Database


class UnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.features = FeatureRepository(session)
        self.artifacts = ArtifactRepository(session)
        self.approvals = ApprovalRepository(session)
        self.logs = ExecutionLogRepository(session)
        self.profiles = BusinessProfileRepository(session)
        self.context = ContextRepository(session)

    async def flush(self) -> None:
        await self.session.flush()


@asynccontextmanager
async def unit_of_work(db: Database) -> AsyncIterator[UnitOfWork]:
    """`async with unit_of_work(db) as uow:` → commit al salir, rollback ante error."""
    async with db.session() as session:
        yield UnitOfWork(session)
