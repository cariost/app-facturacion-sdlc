"""Checkpointer de LangGraph (`AsyncPostgresSaver`) sobre un pool psycopg3 dedicado.

Los checkpoints guardan solo el `FeatureDeliveryState` liviano (punteros + contadores);
el contenido de los artefactos vive en `feature_artifacts`. Cada ejecución usa el
`thread_id` canónico `sf:<feature_id>:v<bp>:<run>`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from specforge.core.config import Settings


@asynccontextmanager
async def open_checkpointer(
    settings: Settings, *, setup: bool = False
) -> AsyncIterator[AsyncPostgresSaver]:
    """Abre el pool y entrega un `AsyncPostgresSaver` listo para `graph.compile(...)`.

    `setup=True` crea/actualiza las tablas de checkpoints (idempotente); en producción se
    ejecuta una vez con `specforge-db migrate --with-checkpointer`.
    """
    # Requisitos de AsyncPostgresSaver: autocommit y sin prepared statements (pgbouncer).
    connection_kwargs: dict[str, Any] = {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=settings.libpq_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        kwargs=connection_kwargs,
        open=False,
    )
    await pool.open(wait=True)
    try:
        saver = AsyncPostgresSaver(pool)
        if setup:
            await saver.setup()
        yield saver
    finally:
        await pool.close()


def thread_config(thread_id: str) -> dict[str, dict[str, str]]:
    """`config` de LangGraph para invocar/reanudar un hilo concreto."""
    return {"configurable": {"thread_id": thread_id}}
