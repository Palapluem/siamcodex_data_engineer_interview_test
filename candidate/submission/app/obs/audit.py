"""Append-only evidence that access control ran.

The brief asks for audit evidence, and a denial that leaves no trace is the one
you most want a record of. Both outcomes are written. The row carries the
identity fixture's subject claim, never a token and never a contact.
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

_INSERT = """
INSERT INTO meridian.access_audit (sub, role, route, decision, reason, http_status, unit_filter)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


async def record(
    pool: AsyncConnectionPool,
    *,
    route: str,
    http_status: int,
    sub: str | None = None,
    role: str | None = None,
    reason: str | None = None,
    unit_filter: str | None = None,
) -> None:
    """Write one audit row.

    Auditing must never be the reason a permitted request fails, so a write
    problem is logged loudly and swallowed rather than raised. The log line is
    the fallback record.
    """
    decision = "allow" if http_status < 400 else "deny"
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_INSERT, (sub, role, route, decision, reason, http_status, unit_filter))
            await conn.commit()
    except Exception:
        log.exception("audit write failed", extra={
            "route": route, "decision": decision, "http_status": http_status,
            "reason": reason, "sub": sub,
        })
