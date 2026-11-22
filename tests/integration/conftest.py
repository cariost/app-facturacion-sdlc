"""Fixtures de integración: una BD efímera por módulo de tests (migrada y eliminada al final).

Requiere `SPECFORGE_TEST_DATABASE_URL` apuntando a una BD administrativa (p. ej. `postgres`)
de un servidor con pgvector y permiso CREATEDB. Sin la variable, los tests se omiten.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from specforge.core.config import Settings, normalize_dsn
from specforge.db import Database, UnitOfWork, unit_of_work
from specforge.db.migrate import apply_migrations

T = TypeVar("T")
ADMIN_DSN = os.environ.get("SPECFORGE_TEST_DATABASE_URL")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


def _with_dbname(dsn: str, dbname: str) -> str:
    parts = urlsplit(normalize_dsn(dsn))
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    if not ADMIN_DSN:
        pytest.skip("SPECFORGE_TEST_DATABASE_URL no definida")
    name = f"specforge_test_{uuid.uuid4().hex[:10]}"
    admin = normalize_dsn(ADMIN_DSN)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = _with_dbname(admin, name)
    try:
        asyncio.run(apply_migrations(dsn))
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(_env_file=None, env="test", database_url=database_url)  # type: ignore[call-arg]


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings)
    yield database
    await database.dispose()


@pytest.fixture
def tx(db: Database) -> Callable[[Callable[[UnitOfWork], Awaitable[T]]], Awaitable[T]]:
    """Ejecuta `fn(uow)` en su propia transacción (commit al salir)."""

    async def run(fn: Callable[[UnitOfWork], Awaitable[T]]) -> T:
        async with unit_of_work(db) as uow:
            return await fn(uow)

    return run


def one_hot(index: int, dim: int = 1536) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


# ------------------------------------------------------------ orquestador (Paso 5)

PROFILE = {
    "tenant_id": "acme",
    "code": "taller-automotriz",
    "display_name": "Taller Acme",
    "industry": "automotriz",
    "config": {
        "reminders": {
            "time": {"interval": "P6M"},
            "usage": {"counter": "odometro_km", "threshold": 5000},
        },
        "inquiries": {"handoff_after_turns": 3},
    },
}


@pytest.fixture
async def checkpointer(settings: Settings):
    from specforge.db.checkpointer import open_checkpointer

    async with open_checkpointer(settings, setup=True) as saver:
        yield saver


@pytest.fixture
def llm():
    from tests.fakes import NodeScriptedLLMClient

    return NodeScriptedLLMClient()


@pytest.fixture
async def orchestrator(db: Database, checkpointer, llm, tx):
    from specforge.context import ContextBuilder, HashingEmbedder
    from specforge.context_packs import seed_context_packs
    from specforge.graph import SpecForgeOrchestrator
    from specforge.llm import ModelRouter, RoutingPolicy
    from tests.fakes import StepClock

    embedder = HashingEmbedder()
    await tx(lambda uow: seed_context_packs(uow, embedder=embedder))
    await tx(lambda uow: uow.profiles.upsert(**PROFILE))
    router = ModelRouter(
        db=db,
        client=llm,
        policy=RoutingPolicy.from_settings(Settings(_env_file=None)),  # type: ignore[call-arg]
        clock=StepClock(),
    )
    return SpecForgeOrchestrator.build(
        db=db,
        checkpointer=checkpointer,
        context=ContextBuilder(embedder=embedder),
        router=router,
        embedder=embedder,
    )
