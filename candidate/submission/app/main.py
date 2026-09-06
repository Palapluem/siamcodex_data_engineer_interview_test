"""Application entry point: lifespan, wiring, and the liveness route."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .api.auth import IdentityClient
from .api.deps import ApiError, error_response
from .api.routes import router
from .config import Settings, load_settings
from .db import build_pool, run_migrations, seed_source_state
from .ingest.client import build_ssl_context
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

    identity: IdentityClient | None = None
    if settings.runs_api:
        identity = IdentityClient(settings, build_ssl_context(settings.lab_ca_path))
        app.state.identity = identity

    supervisor: IngestionSupervisor | None = None
    if settings.runs_worker:
        supervisor = IngestionSupervisor(settings, pool)
        await supervisor.start()

    try:
        yield
    finally:
        if supervisor is not None:
            await supervisor.stop()
        if identity is not None:
            await identity.aclose()
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


@app.exception_handler(ApiError)
async def api_error_handler(_: object, exc: ApiError) -> JSONResponse:
    """Routes raise ApiError after auditing; this renders the stable body."""
    return error_response(exc.status, exc.error)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness only.

    Deliberately does not touch the database or the sources: the contract states
    that /health does not establish source freshness, and a health check that
    fails when a dependency blips causes restarts that fix nothing. Freshness
    lives on the operator-only /status route.
    """
    return JSONResponse({"status": "ok"})


# Routes are mounted only for a role that serves the API. A worker-only
# deployment still exposes /health so its container health check works.
if os.environ.get("ROLE", "all").strip().lower() in {"all", "api"}:
    app.include_router(router)
