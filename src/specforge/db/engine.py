"""Engine async (SQLAlchemy 2.0 + psycopg3) y traducción de errores de BD a dominio."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, NoReturn

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from specforge.core.errors import (
    CONSTRAINT_ERRORS,
    SQLSTATE_ERRORS,
    IntegrityViolationError,
    SpecForgeError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from specforge.core.config import Settings


def translate_db_error(exc: DBAPIError) -> SpecForgeError | None:
    """Mapea SQLSTATE propios (SF00x) y constraints nombrados a errores de dominio."""
    orig = exc.orig
    sqlstate: str | None = getattr(orig, "sqlstate", None)
    if sqlstate is None:
        return None
    diag = getattr(orig, "diag", None)
    message: str = getattr(diag, "message_primary", None) or str(orig)
    if sqlstate in SQLSTATE_ERRORS:
        return SQLSTATE_ERRORS[sqlstate](message)
    if sqlstate.startswith("23"):
        constraint: str | None = getattr(diag, "constraint_name", None)
        error_cls = CONSTRAINT_ERRORS.get(constraint or "", IntegrityViolationError)
        return error_cls(f"{constraint or sqlstate}: {message}")
    return None


def raise_translated(exc: DBAPIError) -> NoReturn:
    domain = translate_db_error(exc)
    if domain is None:
        raise exc
    raise domain from exc


class Database:
    """Dueño del engine y la fábrica de sesiones. Una instancia por proceso."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_async_engine(
            settings.sqlalchemy_url,
            pool_size=settings.db_pool_max_size,
            max_overflow=0,
            pool_pre_ping=True,
        )
        self._sessionmaker = async_sessionmaker(
            self.engine, expire_on_commit=False, autoflush=False
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Sesión transaccional: commit al salir, rollback ante error, errores traducidos."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except DBAPIError as exc:
                await session.rollback()
                raise_translated(exc)
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
