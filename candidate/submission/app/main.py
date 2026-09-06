"""Application entry point: lifespan, wiring, and the liveness route."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .db import build_pool, run_migrations, seed_source_state
from .ingest.poller import IngestionSupervisor
from .obs import logging as obs_logging

log = logging.getLogger("meridian")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = load_settings()
    obs_logging.configure(settings.log_level)
    app.state.settings = settings

    pool = build_pool(settings)
    await pool.open(wait=True, timeout=30.0)
    app.state.pool = pool

    async with pool.connection() as conn:
        applied = await run_migrations(conn)
        await seed_source_state(conn, settings.sources)
    log.info(
        "startup complete",
        extra={"role": settings.role, "migrations_applied": applied,
               "business_timezone": str(settings.business_timezone),
               "clearance_filters_aggregates": settings.clearance_filters_aggregates},
    )

    supervisor: IngestionSupervisor | None = None
    if settings.runs_worker:
        supervisor = IngestionSupervisor(settings, pool)
        await supervisor.start()

    try:
        yield
    finally:
        if supervisor is not None:
            await supervisor.stop()
        await pool.close()
        log.info("shutdown complete")


app = FastAPI(
    title="Meridian operational view",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,      # no interactive docs: an unauthenticated route that
    redoc_url=None,     # enumerates the API is a needless disclosure
    openapi_url=None,
)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness only.

    Deliberately does not touch the database or the sources: the contract states
    that /health does not establish source freshness, and a health check that
    fails when a dependency blips causes restarts that fix nothing. Freshness
    lives on the operator-only /status route.
    """
    return JSONResponse({"status": "ok"})
