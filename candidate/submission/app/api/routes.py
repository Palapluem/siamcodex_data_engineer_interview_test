"""Result API routes: /status, /reports/daily, /cases.

Response shapes are fixed by the technical contract. Row objects carry exactly
the specified fields and nothing else, because an extra field inside a row is a
leak the caller never asked for.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..obs import audit
from .auth import Principal
from .deps import ApiError, error_response, guard

log = logging.getLogger(__name__)
router = APIRouter()


def utc_iso(value: datetime | None) -> str | None:
    """UTC ISO 8601 with a Z suffix.

    Explicitly UTC rather than a bare .astimezone(), which resolves to the
    container's local zone - UTC here only by accident of the base image.
    Both routes share this so the API never emits two timestamp formats.
    """
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")



_STATUS_SQL = """
SELECT source, cursor, state, last_success_at
FROM meridian.source_state ORDER BY source
"""

_QUARANTINE_SQL = "SELECT count(*) FROM meridian.quarantine"

# Aggregates only live rows, only permitted units, and - when clearance filters
# aggregates - only classifications the caller may see. Sorting is fixed by the
# contract: date, unit_id, status.
_REPORT_SQL = """
SELECT (event_time AT TIME ZONE %(tz)s)::date AS date,
       unit_id,
       status,
       count(*)          AS case_count,
       sum(amount_minor) AS amount_minor
FROM meridian.case_current
WHERE NOT is_deleted
  AND unit_id = ANY(%(units)s)
  AND (%(all_classifications)s OR classification = 'internal')
  AND event_time IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

_CASE_SQL = """
SELECT source, case_id, unit_id, version, event_time, amount_minor,
       currency, status, classification, contact
FROM meridian.case_current
WHERE source = %s AND case_id = %s AND NOT is_deleted
"""


@router.get("/status")
async def status(request: Request) -> JSONResponse:
    """Operator-only operational view. Never business data, never credentials."""
    caller = await guard(request, "status")
    pool = request.app.state.pool

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_STATUS_SQL)
        rows = await cur.fetchall()
        await cur.execute(_QUARANTINE_SQL)
        quarantine_count = (await cur.fetchone())[0]

    sources = {
        source: {
            "cursor": cursor,
            "state": state,
            "last_success_at": utc_iso(last_success),
        }
        for source, cursor, state, last_success in rows
    }

    await audit.record(pool, route="status", http_status=200,
                       sub=caller.principal.sub, role=caller.principal.role)
    return JSONResponse({"sources": sources, "quarantine_count": quarantine_count})


@router.get("/reports/daily")
async def reports_daily(request: Request, unit_id: str | None = None) -> JSONResponse:
    caller = await guard(request, "reports")
    principal: Principal = caller.principal
    settings = caller.settings
    pool = request.app.state.pool

    if unit_id is not None:
        # Validate against the allowlist first: an unknown input must never
        # reach the query, and 400 for unknown is distinct from 403 for known
        # but not permitted.
        if unit_id not in settings.known_units:
            await audit.record(pool, route="reports", http_status=400, sub=principal.sub,
                               role=principal.role, reason="unknown_unit", unit_filter=unit_id)
            raise ApiError(400, "unknown_unit")
        # Authorisation is checked before data existence. The other order turns
        # 403 into a 404 oracle that reveals which units hold cases.
        if not principal.may_see_unit(unit_id):
            await audit.record(pool, route="reports", http_status=403, sub=principal.sub,
                               role=principal.role, reason="unit", unit_filter=unit_id)
            raise ApiError(403, "forbidden", "unit")
        units = [unit_id]
    else:
        units = principal.permitted_units(settings.known_units)

    all_classifications = principal.sees_restricted or not settings.clearance_filters_aggregates

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_REPORT_SQL, {
            "tz": str(settings.business_timezone),
            "units": units,
            "all_classifications": all_classifications,
        })
        rows = await cur.fetchall()

    await audit.record(pool, route="reports", http_status=200, sub=principal.sub,
                       role=principal.role, unit_filter=unit_id)

    # One row per non-empty group, no zero filling; amounts are integers.
    return JSONResponse({"rows": [
        {
            "date": date.isoformat(),
            "unit_id": unit,
            "status": row_status,
            "case_count": int(case_count),
            "amount_minor": int(amount_minor),
        }
        for date, unit, row_status, case_count, amount_minor in rows
    ]})


@router.get("/cases")
async def case_detail(request: Request, source: str | None = None,
                      case_id: str | None = None) -> JSONResponse:
    caller = await guard(request, "cases")
    principal: Principal = caller.principal
    settings = caller.settings
    pool = request.app.state.pool

    if source is None or source not in settings.sources:
        await audit.record(pool, route="cases", http_status=400, sub=principal.sub,
                           role=principal.role, reason="unknown_source")
        raise ApiError(400, "unknown_source")
    if not case_id:
        await audit.record(pool, route="cases", http_status=400, sub=principal.sub,
                           role=principal.role, reason="missing_case_id")
        raise ApiError(400, "missing_case_id")

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_CASE_SQL, (source, case_id))
        row = await cur.fetchone()

    if row is None:
        # A tombstoned case is indistinguishable from one that never existed,
        # which is the point: a 404 must not confirm that a deleted case was real.
        await audit.record(pool, route="cases", http_status=404, sub=principal.sub,
                           role=principal.role, reason="not_found")
        raise ApiError(404, "not_found")

    unit_id = row[2]
    if unit_id is not None and not principal.may_see_unit(unit_id):
        await audit.record(pool, route="cases", http_status=403, sub=principal.sub,
                           role=principal.role, reason="unit", unit_filter=unit_id)
        raise ApiError(403, "forbidden", "unit")

    (src, cid, unit, version, event_time, amount_minor,
     currency, row_status, classification, contact) = row

    await audit.record(pool, route="cases", http_status=200, sub=principal.sub,
                       role=principal.role, unit_filter=unit)

    # Exactly the ten fields the contract specifies, in that order.
    return JSONResponse({"item": {
        "source": src,
        "case_id": cid,
        "unit_id": unit,
        "version": version,
        "event_time": utc_iso(event_time),
        "amount_minor": int(amount_minor) if amount_minor is not None else None,
        "currency": currency,
        "status": row_status,
        "classification": classification,
        "contact": contact,
    }})


__all__ = ["router", "error_response"]
