"""Shared request plumbing: authentication, authorisation, audit, errors.

Every business route goes through `guard`, so no route can be added that
forgets to authorise or to leave audit evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..obs import audit
from .auth import (
    IdentityUnavailable,
    Principal,
    Unauthenticated,
    authorize,
    extract_bearer,
)


class ApiError(Exception):
    """A response the route wants to return, already audited."""

    def __init__(self, status: int, error: str, reason: str | None = None) -> None:
        self.status = status
        self.error = error
        self.reason = reason
        super().__init__(error)


def error_response(status: int, error: str) -> JSONResponse:
    # The body carries a stable code and nothing else: an error message is a
    # place sensitive values leak, and the contract requires redacting them too.
    return JSONResponse({"error": error}, status_code=status)


@dataclass(frozen=True)
class Caller:
    principal: Principal
    settings: Settings


async def guard(request: Request, route: str) -> Caller:
    """Authenticate, authorise, audit. Raises ApiError on any denial."""
    settings: Settings = request.app.state.settings
    pool = request.app.state.pool
    identity = request.app.state.identity

    try:
        token = extract_bearer(request.headers.get("Authorization"))
        principal = await identity.introspect(token)
    except Unauthenticated as exc:
        await audit.record(pool, route=route, http_status=401, reason=str(exc))
        raise ApiError(401, "unauthenticated") from exc
    except IdentityUnavailable as exc:
        # Fail closed: without a verifiable identity we cannot know what this
        # caller may see, and guessing is the one unacceptable answer.
        await audit.record(pool, route=route, http_status=503, reason="identity_unavailable")
        raise ApiError(503, "identity_unavailable") from exc

    decision = authorize(principal, route)
    if not decision.allowed:
        await audit.record(pool, route=route, http_status=403, sub=principal.sub,
                           role=principal.role, reason=decision.reason)
        raise ApiError(403, "forbidden", decision.reason)

    return Caller(principal=principal, settings=settings)
