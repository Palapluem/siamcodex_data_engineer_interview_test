"""Connection pool and startup migrations."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from .config import Settings

log = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).resolve().parent / "migrations"

_LEDGER = """
CREATE SCHEMA IF NOT EXISTS meridian;
CREATE TABLE IF NOT EXISTS meridian.schema_migration (
    filename    text PRIMARY KEY,
    sha256      text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def build_pool(settings: Settings) -> AsyncConnectionPool:
    """Pool sized for three pollers plus concurrent report readers.

    max_size stays well under the server's max_connections=30 so a restart storm
    cannot exhaust the database before the health check notices.
    """
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=2,
        max_size=10,
        timeout=10.0,
        max_idle=300.0,
        open=False,
        kwargs={"application_name": "meridian-pipeline"},
    )


async def run_migrations(conn: AsyncConnection) -> list[str]:
    """Apply pending migrations in filename order, inside one transaction each.

    Re-running is a no-op. A file whose content changed after being applied is an
    error rather than a silent skip: editing an applied migration is how two
    environments quietly stop matching.
    """
    async with conn.cursor() as cur:
        await cur.execute(_LEDGER)
        await conn.commit()

        await cur.execute("SELECT filename, sha256 FROM meridian.schema_migration")
        applied = {row[0]: row[1] for row in await cur.fetchall()}

    performed: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        body = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

        if path.name in applied:
            if applied[path.name] != digest:
                raise RuntimeError(
                    f"migration {path.name} changed after it was applied; "
                    "add a new migration instead of editing this one"
                )
            continue

        async with conn.cursor() as cur:
            await cur.execute(body)
            await cur.execute(
                "INSERT INTO meridian.schema_migration (filename, sha256) VALUES (%s, %s)",
                (path.name, digest),
            )
        await conn.commit()
        performed.append(path.name)
        log.info("applied migration %s", path.name)

    return performed


async def seed_source_state(conn: AsyncConnection, sources: tuple[str, ...]) -> None:
    """Ensure one state row per source. Existing cursors are never reset."""
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO meridian.source_state (source) VALUES (%s) ON CONFLICT (source) DO NOTHING",
            [(source,) for source in sources],
        )
    await conn.commit()
