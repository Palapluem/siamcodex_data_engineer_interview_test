"""One independent polling loop per source.

Each source owns its task, client, rate budget and backoff. Nothing is awaited
across sources, so a link that is down for thirty seconds costs exactly one
source its freshness and nothing else.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import AsyncConnectionPool

from ..config import Settings
from .apply import apply_page, load_cursor, record_failure
from .client import Outcome, SourceClient, backoff_delay, build_ssl_context, load_source_credentials

log = logging.getLogger(__name__)

# Two failures can be a single injected fault plus its retry, which is normal
# operation here. Three consecutive failures is a real signal.
DEGRADE_AFTER = 3


class SourcePoller:
    def __init__(self, source: str, client: SourceClient, pool: AsyncConnectionPool,
                 settings: Settings) -> None:
        self.source = source
        self._client = client
        self._pool = pool
        self._settings = settings
        self._failures = 0

    async def run(self) -> None:
        log.info("poller started", extra={"source": self.source})
        while True:
            try:
                idle = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A poller must never die: an unhandled error here would silently
                # freeze one source for the lifetime of the process.
                log.exception("poller iteration failed", extra={"source": self.source})
                idle = backoff_delay(self._failures)
                self._failures += 1
            if idle:
                await asyncio.sleep(idle)

    async def _tick(self) -> float:
        """One fetch-and-apply. Returns how long to sleep before the next one."""
        async with self._pool.connection() as conn:
            cursor = await load_cursor(conn, self.source)

        page = await self._client.fetch(cursor)

        if page.ok:
            async with self._pool.connection() as conn:
                result = await apply_page(conn, self.source, page.items, page.next_cursor)
            self._failures = 0
            if result.accepted or result.rejected:
                log.info("page applied", extra={
                    "source": self.source, "cursor": cursor, "next_cursor": result.cursor,
                    "accepted": result.accepted, "rejected": result.rejected,
                    "has_more": page.has_more,
                })
            # Keep polling after has_more=false: the contract says new events
            # become visible later, so "caught up" is never "finished".
            return 0.0 if page.has_more else self._settings.poll_idle_seconds

        return await self._handle_failure(page)

    async def _handle_failure(self, page) -> float:
        self._failures += 1
        degrade = self._failures >= DEGRADE_AFTER

        async with self._pool.connection() as conn:
            await record_failure(conn, self.source, page.outcome.value, degrade=degrade)

        if page.outcome is Outcome.AUTH:
            # A rotated key is not a retryable error, and hammering it just adds
            # failed auth attempts to the source's logs. Back off hard and pick
            # up the new credential from the mounted file.
            self._reload_credential()
            delay = 30.0
        elif page.outcome is Outcome.BAD_REQUEST:
            # We built a query the fixture rejects. Retrying unchanged cannot
            # help, so make it loud rather than silently looping.
            log.error("source rejected our request", extra={
                "source": self.source, "detail": page.detail})
            delay = 30.0
        elif page.retry_after is not None:
            # Honour the server's hint, then add jitter so three sources that
            # fail together do not retry in lockstep.
            delay = page.retry_after + backoff_delay(self._failures - 1, base=0.25, cap=5.0)
        else:
            delay = backoff_delay(self._failures - 1)

        log.warning("poll failed", extra={
            "source": self.source, "outcome": page.outcome.value, "detail": page.detail,
            "consecutive_failures": self._failures, "retry_in_seconds": round(delay, 2),
            "degraded": degrade,
        })
        return delay

    def _reload_credential(self) -> None:
        try:
            credentials = load_source_credentials(self._settings.source_credentials_path)
            self._client.update_token(credentials[self.source]["token"])
            log.info("reloaded source credential", extra={"source": self.source})
        except (OSError, KeyError, ValueError) as exc:
            log.error("could not reload source credential", extra={
                "source": self.source, "error": type(exc).__name__})


class IngestionSupervisor:
    """Owns the client and task per source, and shuts them down cleanly."""

    def __init__(self, settings: Settings, pool: AsyncConnectionPool) -> None:
        self._settings = settings
        self._pool = pool
        self._clients: list[SourceClient] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        ssl_context = build_ssl_context(self._settings.lab_ca_path)
        credentials = load_source_credentials(self._settings.source_credentials_path)

        for source in self._settings.sources:
            config = credentials[source]
            client = SourceClient(source, config["url"], config["token"], self._settings, ssl_context)
            self._clients.append(client)
            poller = SourcePoller(source, client, self._pool, self._settings)
            self._tasks.append(asyncio.create_task(poller.run(), name=f"poller-{source}"))

        log.info("ingestion started", extra={"sources": list(self._settings.sources)})

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for client in self._clients:
            await client.aclose()
        log.info("ingestion stopped")
