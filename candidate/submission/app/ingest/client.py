"""Source gateway client: TLS, rate budget, and error classification.

One client instance per source. Nothing is shared between sources - not the
connection pool, not the rate limiter, not the backoff state - because the brief
says one business's connection often fails while the others remain available,
and shared machinery is how a single dead link stalls everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"   # 429, self-inflicted or contended
    TRANSIENT = "transient"         # 503 injected fault or gateway upstream error
    OUTAGE = "outage"               # 503 link_unavailable: the source link is down
    AUTH = "auth"                   # 401: wrong or rotated key
    BAD_REQUEST = "bad_request"     # 400: our query construction is wrong
    NETWORK = "network"             # timeout, connection reset, TLS failure
    MALFORMED = "malformed"         # 200 with a body we cannot parse


@dataclass(frozen=True)
class Page:
    outcome: Outcome
    items: list[dict[str, Any]]
    next_cursor: str
    has_more: bool
    retry_after: float | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


class TokenBucket:
    """Sliding-rate limiter kept deliberately below the published ceiling.

    The ceiling is enforced source-wide across all connections, so crowding it
    buys nothing and costs a 429 plus a full second of Retry-After.
    """

    def __init__(self, rate_per_second: float) -> None:
        self._interval = 1.0 / rate_per_second
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._interval


def load_source_credentials(path: Path) -> dict[str, dict[str, str]]:
    """Read the mounted credential file. Called at startup and after a 401."""
    return json.loads(path.read_text(encoding="utf-8"))


def build_ssl_context(ca_path: Path) -> ssl.SSLContext:
    """Trust the lab CA only, and verify the hostname.

    create_default_context already sets check_hostname and CERT_REQUIRED; they
    are asserted here so that a future edit cannot quietly weaken verification.
    """
    context = ssl.create_default_context(cafile=str(ca_path))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class SourceClient:
    def __init__(self, source: str, url: str, token: str, settings: Settings,
                 ssl_context: ssl.SSLContext) -> None:
        self.source = source
        self.url = url
        self._token = token
        self._settings = settings
        self._bucket = TokenBucket(settings.rate_budget[source])
        self._client = httpx.AsyncClient(
            verify=ssl_context,
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            # One in-flight request per source: the ceiling is source-wide, so
            # concurrency here would only produce 429s.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"Accept": "application/json"},
        )

    def update_token(self, token: str) -> None:
        self._token = token

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, cursor: str) -> Page:
        await self._bucket.acquire()
        # Exactly cursor and limit: the fixture rejects any other query key with
        # a 400, so this is not a place to add tracing parameters.
        params = {"cursor": cursor, "limit": str(self._settings.page_limit)}

        try:
            response = await self._client.get(
                self.url, params=params, headers={"Authorization": f"Bearer {self._token}"}
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return Page(Outcome.NETWORK, [], cursor, False, detail=type(exc).__name__)

        if response.status_code == 200:
            return self._parse(response, cursor)

        retry_after = _retry_after(response)
        if response.status_code == 429:
            return Page(Outcome.RATE_LIMITED, [], cursor, False, retry_after or 1.0)
        if response.status_code == 401:
            return Page(Outcome.AUTH, [], cursor, False, detail="unauthenticated")
        if response.status_code == 400:
            return Page(Outcome.BAD_REQUEST, [], cursor, False, detail="invalid_cursor_or_limit")
        if response.status_code == 503:
            # link_unavailable means the source link itself is down; the other
            # 503s are the injected transient fault or a gateway upstream blip.
            error = self._error_code(response)
            outcome = Outcome.OUTAGE if error == "link_unavailable" else Outcome.TRANSIENT
            return Page(outcome, [], cursor, False, retry_after or 1.0, detail=error)
        return Page(Outcome.TRANSIENT, [], cursor, False, retry_after or 1.0,
                    detail=f"http_{response.status_code}")

    def _parse(self, response: httpx.Response, cursor: str) -> Page:
        try:
            body = response.json()
            items = body["items"]
            next_cursor = str(body["next_cursor"])
            has_more = bool(body["has_more"])
        except (ValueError, KeyError, TypeError) as exc:
            return Page(Outcome.MALFORMED, [], cursor, False, detail=type(exc).__name__)

        if not isinstance(items, list):
            return Page(Outcome.MALFORMED, [], cursor, False, detail="items_not_a_list")

        # An empty page leaves the cursor unchanged, per the contract. Guard
        # against ever moving backwards, which would replay work forever.
        try:
            if int(next_cursor) < int(cursor):
                next_cursor = cursor
        except ValueError:
            return Page(Outcome.MALFORMED, [], cursor, False, detail="non_numeric_cursor")

        return Page(Outcome.OK, items, next_cursor, has_more)

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            return str(response.json().get("error", ""))
        except ValueError:
            return ""


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter.

    Jitter matters even with three clients: without it, three sources that fail
    together retry together forever.
    """
    return random.uniform(0.0, min(cap, base * (2 ** min(attempt, 6))))
