"""Runner de migraciones SQL versionadas + CLI `specforge-db`.

- Aplica `migrations/NNNN_nombre.sql` en orden, cada una en su propia transacción.
- Registra versión + checksum SHA-256 en `schema_migrations`; si una migración ya
  aplicada cambió de contenido, falla (las migraciones son inmutables).
- Serializa ejecuciones concurrentes con `pg_advisory_lock`.
- `--with-checkpointer` crea además las tablas de checkpoints de LangGraph.

Uso:
    specforge-db migrate [--with-checkpointer] [--database-url DSN]
    specforge-db status  [--database-url DSN]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

import psycopg

from specforge.core.config import Settings, get_settings, normalize_dsn
from specforge.core.errors import MigrationError

if TYPE_CHECKING:
    from collections.abc import Sequence

_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_ADVISORY_LOCK_KEY = 0x5F_5350_4543  # "_SPEC": constante del proceso de migración

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover_migrations() -> list[Migration]:
    directory = files("specforge.db").joinpath("migrations")
    migrations: list[Migration] = []
    for entry in directory.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"nombre de migración inválido: {entry.name}")
        migrations.append(
            Migration(match["version"], match["name"], entry.read_text(encoding="utf-8"))
        )
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"versiones de migración duplicadas: {versions}")
    return migrations


async def _applied(conn: psycopg.AsyncConnection[tuple[str, str]]) -> dict[str, str]:
    cur = await conn.execute("SELECT version, checksum FROM schema_migrations")
    return {version: checksum for version, checksum in await cur.fetchall()}


async def apply_migrations(dsn: str) -> list[str]:
    """Aplica las migraciones pendientes. Devuelve las versiones aplicadas en esta corrida."""
    applied_now: list[str] = []
    conn: psycopg.AsyncConnection[tuple[str, str]]
    async with await psycopg.AsyncConnection.connect(normalize_dsn(dsn), autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        try:
            await conn.execute(_CREATE_LEDGER)
            applied = await _applied(conn)
            for migration in discover_migrations():
                recorded = applied.get(migration.version)
                if recorded is not None:
                    if recorded != migration.checksum:
                        raise MigrationError(
                            f"la migración {migration.version}_{migration.name} cambió "
                            "después de aplicarse; crea una nueva migración en su lugar"
                        )
                    continue
                async with conn.transaction():
                    # Sin parámetros psycopg permite múltiples sentencias en un execute.
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum)"
                        " VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
                applied_now.append(migration.version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    return applied_now


async def migration_status(dsn: str) -> list[tuple[Migration, bool]]:
    conn: psycopg.AsyncConnection[tuple[str, str]]
    async with await psycopg.AsyncConnection.connect(normalize_dsn(dsn), autocommit=True) as conn:
        cur = await conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
        row = await cur.fetchone()
        applied = await _applied(conn) if row and row[0] else {}
    return [(m, m.version in applied) for m in discover_migrations()]


async def setup_checkpointer(settings: Settings) -> None:
    from specforge.db.checkpointer import open_checkpointer

    async with open_checkpointer(settings, setup=True):
        pass


async def _seed_packs(settings: Settings) -> int:
    from specforge.context.embeddings import HashingEmbedder
    from specforge.context_packs import seed_context_packs
    from specforge.db import Database, unit_of_work

    db = Database(settings)
    try:
        async with unit_of_work(db) as uow:
            results = await seed_context_packs(uow, embedder=HashingEmbedder())
    finally:
        await db.dispose()
    for r in results:
        status = f"+{r.chunks_added} chunks" if r.changed else "sin cambios"
        print(f"{r.pack_code}@{r.version}: {status}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    dsn = args.database_url or settings.libpq_url
    if args.command == "migrate":
        applied = await apply_migrations(dsn)
        print(f"migraciones aplicadas: {', '.join(applied) if applied else 'ninguna pendiente'}")
        if args.with_checkpointer:
            await setup_checkpointer(settings.model_copy(update={"database_url": dsn}))
            print("tablas de checkpoints de LangGraph listas")
        return 0
    if args.command == "seed-packs":
        return await _seed_packs(settings.model_copy(update={"database_url": dsn}))
    for migration, is_applied in await migration_status(dsn):
        mark = "x" if is_applied else " "
        print(f"[{mark}] {migration.version}_{migration.name}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="specforge-db", description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", help="DSN (por defecto SPECFORGE_DATABASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_cmd = sub.add_parser("migrate", help="aplica migraciones pendientes")
    migrate_cmd.add_argument(
        "--with-checkpointer", action="store_true", help="crea las tablas de LangGraph"
    )
    sub.add_parser("status", help="muestra migraciones aplicadas/pendientes")
    sub.add_parser("seed-packs", help="publica los Context Packs iniciales y sus embeddings")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
