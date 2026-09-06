"""Identity introspection and the access-policy table.

Two principles the contract fixes and this module enforces:

- Claims come only from the identity fixture. A caller-supplied X-Role or
  X-Units header is data, never authority, so nothing here reads request headers
  other than Authorization.
- Fail closed. When identity cannot be verified and no valid cache entry
  remains, a business route returns 503 rather than guessing.

The policy itself is a table. The interviewer supplies a clarification sheet
after implementation and may introduce a bounded change during review; a table
makes that a one-line edit that can be made live.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

# Purposes permitted to reach business data at all. The wrong_purpose fixture is
# an analyst with full scope and purpose=marketing: it exists to be denied.
ALLOWED_PURPOSES = frozenset({"operations", "audit"})

# route -> roles permitted. /status is operational visibility, which the brief
# separates from business access: "an operator who restarts jobs should not
# automatically gain access to business data".
ROUTE_ROLES: dict[str, frozenset[str]] = {
    "status": frozenset({"operator"}),
    "reports": frozenset({"analyst", "auditor"}),
    "cases": frozenset({"auditor"}),
}

# Routes that read business data, so they also require an allowed purpose.
BUSINESS_ROUTES = frozenset({"reports", "cases"})


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    units: tuple[str, ...]
    clearance: str
    purpose: str

    @property
    def all_units(self) -> bool:
        return "*" in self.units

    def may_see_unit(self, unit: str) -> bool:
        return self.all_units or unit in self.units

    def permitted_units(self, known_units: frozenset[str]) -> list[str]:
        if self.all_units:
            return sorted(known_units)
        return sorted(u for u in self.units if u in known_units)

    @property
    def sees_restricted(self) -> bool:
        return self.clearance == "restricted"


class Unauthenticated(Exception):
    """No usable credential: missing, malformed, unknown or revoked -> 401."""


class IdentityUnavailable(Exception):
    """Identity could not be reached and no valid cache entry remains -> 503."""


@dataclass
class _CacheEntry:
    principal: Principal | None   # None caches a confirmed-inactive token
    expires_at: float


class IdentityClient:
    """Introspection with a short TTL cache.

    The cache is keyed by a SHA-256 of the token, never the token itself, so a
    memory dump or an accidental repr cannot yield a working credential.
    """

    def __init__(self, settings: Settings, ssl_context: ssl.SSLContext) -> None:
        self._settings = settings
        self._url = settings.identity_url
        self._service_key = self._read_key(settings.identity_service_key_path)
        self._ttl = settings.introspect_cache_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            verify=ssl_context,
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    @staticmethod
    def _read_key(path: Path) -> str:
        return path.read_text(encoding="utf-8").strip()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def introspect(self, token: str) -> Principal:
        key = self._key(token)
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            if cached.principal is None:
                raise Unauthenticated("inactive token")
            return cached.principal

        try:
            response = await self._client.post(
                self._url,
                json={"token": token},
                headers={"Authorization": f"Bearer {self._service_key}",
                         "Content-Type": "application/json"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # The expired entry is deliberately not reused: the contract requires
            # failing closed once a valid cache entry has expired.
            self._cache.pop(key, None)
            raise IdentityUnavailable(type(exc).__name__) from exc

        if response.status_code != 200:
            self._cache.pop(key, None)
            raise IdentityUnavailable(f"http_{response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise IdentityUnavailable("malformed_introspection") from exc

        expires_at = time.monotonic() + self._ttl

        if not body.get("active"):
            async with self._lock:
                self._cache[key] = _CacheEntry(None, expires_at)
            raise Unauthenticated("inactive token")

        try:
            principal = Principal(
                sub=str(body["sub"]),
                role=str(body["role"]),
                units=tuple(str(u) for u in body["units"]),
                clearance=str(body["clearance"]),
                purpose=str(body["purpose"]),
            )
        except (KeyError, TypeError) as exc:
            raise IdentityUnavailable("incomplete_claims") from exc

        async with self._lock:
            self._cache[key] = _CacheEntry(principal, expires_at)
        return principal


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None   # role | purpose | unit | clearance

    @property
    def status(self) -> int:
        return 200 if self.allowed else 403


def authorize(principal: Principal, route: str) -> Decision:
    """Role and purpose check for a route. Unit scoping is applied separately."""
    permitted = ROUTE_ROLES.get(route)
    if permitted is None or principal.role not in permitted:
        return Decision(False, "role")
    if route in BUSINESS_ROUTES and principal.purpose not in ALLOWED_PURPOSES:
        return Decision(False, "purpose")
    return Decision(True)


def extract_bearer(header: str | None) -> str:
    if not header:
        raise Unauthenticated("missing credential")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("malformed credential")
    return token.strip()
