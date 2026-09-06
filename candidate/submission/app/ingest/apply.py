"""Apply one page of events and advance the cursor, atomically.

This module holds the correctness of the whole pipeline. Two rules do the work:

1. Everything for a page - dedupe ledger, lineage, business state, quarantine,
   and the new cursor - commits in ONE transaction. If any part fails, the
   cursor does not move and the page is re-fetched. Advancing the cursor outside
   the transaction is the classic silent data-loss bug.

2. Business state is keyed on (source, case_id) and only a strictly higher
   version wins. That makes applying the same event twice a no-op, which is what
   turns the source's at-least-once delivery into exactly-once business state.

Together they mean a duplicate delivery, a mid-page crash, a container restart
and a full replay from cursor 0 all converge to the same numbers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from .normalize import CanonicalEvent, Rejection, normalize

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageResult:
    accepted: int
    rejected: int
    cursor: str


_UPSERT_CASE = """
INSERT INTO meridian.case_current (
    source, case_id, unit_id, version, event_time, amount_minor, currency,
    status, classification, contact, is_deleted, last_event_id, updated_at)
VALUES (%(source)s, %(case_id)s, %(unit_id)s, %(version)s, %(event_time)s,
        %(amount_minor)s, %(currency)s, %(status)s, %(classification)s,
        %(contact)s, false, %(event_id)s, now())
ON CONFLICT (source, case_id) DO UPDATE SET
    unit_id        = EXCLUDED.unit_id,
    version        = EXCLUDED.version,
    event_time     = EXCLUDED.event_time,
    amount_minor   = EXCLUDED.amount_minor,
    currency       = EXCLUDED.currency,
    status         = EXCLUDED.status,
    classification = EXCLUDED.classification,
    contact        = EXCLUDED.contact,
    is_deleted     = false,
    last_event_id  = EXCLUDED.last_event_id,
    updated_at     = now()
WHERE EXCLUDED.version > meridian.case_current.version
"""

# A tombstone carries identity and version only, so it must not overwrite the
# known attributes with NULL: the assurance team still needs to see what the
# case was. COALESCE keeps prior values when the insert supplies none.
_DELETE_CASE = """
INSERT INTO meridian.case_current (
    source, case_id, version, is_deleted, last_event_id, updated_at)
VALUES (%(source)s, %(case_id)s, %(version)s, true, %(event_id)s, now())
ON CONFLICT (source, case_id) DO UPDATE SET
    version       = EXCLUDED.version,
    is_deleted    = true,
    last_event_id = EXCLUDED.last_event_id,
    updated_at    = now()
WHERE EXCLUDED.version > meridian.case_current.version
"""

_SEEN = """
INSERT INTO meridian.event_seen (source, event_id, first_seq, last_seq)
VALUES (%s, %s, %s, %s)
ON CONFLICT (source, event_id) DO UPDATE SET
    last_seq   = GREATEST(meridian.event_seen.last_seq, EXCLUDED.last_seq),
    deliveries = meridian.event_seen.deliveries + 1
"""

_LINEAGE = """
INSERT INTO meridian.case_event (
    source, event_id, seq, case_id, version, op, schema_version, payload_sha256, applied)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source, event_id, seq) DO NOTHING
"""

# occurrences counts redeliveries of the same bad event, but the primary key
# keeps quarantine_count on "distinct rejected source/event_id pairs" so a
# replay cannot inflate it.
_QUARANTINE = """
INSERT INTO meridian.quarantine (source, event_id, seq, reason_codes, detail)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (source, event_id) DO UPDATE SET
    occurrences  = meridian.quarantine.occurrences + 1,
    last_seen_at = now(),
    reason_codes = EXCLUDED.reason_codes,
    detail       = EXCLUDED.detail
"""

_ADVANCE = """
UPDATE meridian.source_state
SET cursor          = %(cursor)s,
    state           = 'healthy',
    last_success_at = %(now)s,
    last_attempt_at = %(now)s,
    last_error_kind = NULL,
    consecutive_failures = 0,
    events_ingested = events_ingested + %(count)s,
    updated_at      = now()
WHERE source = %(source)s
"""


async def apply_page(
    conn: AsyncConnection,
    source: str,
    items: Sequence[dict[str, Any]],
    next_cursor: str,
) -> PageResult:
    """Normalise, persist and advance in a single transaction."""
    accepted: list[CanonicalEvent] = []
    rejected: list[Rejection] = []

    for item in items:
        result = normalize(source, item)
        (accepted if isinstance(result, CanonicalEvent) else rejected).append(result)  # type: ignore[arg-type]

    now = datetime.now(UTC)

    # psycopg opens a transaction implicitly; this block makes the boundary
    # explicit so the cursor update cannot drift outside it during a later edit.
    async with conn.transaction():
        async with conn.cursor() as cur:
            if accepted:
                await cur.executemany(
                    _SEEN, [(e.source, e.event_id, e.seq, e.seq) for e in accepted]
                )
                await cur.executemany(_LINEAGE, [
                    (e.source, e.event_id, e.seq, e.case_id, e.version, e.op,
                     e.schema_version, e.payload_sha256, True)
                    for e in accepted
                ])

                # Ordering by version means that when one page carries both v1
                # and its v2 correction, the higher version is applied last and
                # wins regardless of delivery order within the page.
                for event in sorted(accepted, key=lambda e: (e.case_id, e.version)):
                    if event.is_delete:
                        await cur.execute(_DELETE_CASE, {
                            "source": event.source, "case_id": event.case_id,
                            "version": event.version, "event_id": event.event_id,
                        })
                    else:
                        await cur.execute(_UPSERT_CASE, {
                            "source": event.source, "case_id": event.case_id,
                            "unit_id": event.unit_id, "version": event.version,
                            "event_time": event.event_time, "amount_minor": event.amount_minor,
                            "currency": event.currency, "status": event.status,
                            "classification": event.classification, "contact": event.contact,
                            "event_id": event.event_id,
                        })

            if rejected:
                await cur.executemany(_QUARANTINE, [
                    (r.source, r.event_id, r.seq, list(r.reason_codes), Jsonb(r.detail))
                    for r in rejected
                ])

            await cur.execute(_ADVANCE, {
                "cursor": next_cursor, "now": now,
                "count": len(accepted), "source": source,
            })

    return PageResult(accepted=len(accepted), rejected=len(rejected), cursor=next_cursor)


async def record_failure(
    conn: AsyncConnection, source: str, error_kind: str, *, degrade: bool
) -> None:
    """Record a failed poll without touching the cursor.

    Kept separate from apply_page so there is no code path where a failure can
    move a cursor.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE meridian.source_state
                SET consecutive_failures = consecutive_failures + 1,
                    last_attempt_at = now(),
                    last_error_kind = %s,
                    state = CASE WHEN %s THEN 'degraded' ELSE state END,
                    updated_at = now()
                WHERE source = %s
                """,
                (error_kind, degrade, source),
            )


async def load_cursor(conn: AsyncConnection, source: str) -> str:
    """Always read the cursor from the database, never from process memory."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT cursor FROM meridian.source_state WHERE source = %s", (source,))
        row = await cur.fetchone()
    return row[0] if row else "0"
