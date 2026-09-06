"""Vendor payloads to one canonical case shape.

Three acquired businesses describe the same thing three ways, and the brief is
explicit that more will follow. Each vendor gets one adapter function; everything
downstream sees only CanonicalEvent.

Validation never raises past this module. An invalid event is data, not a
control-flow problem: it becomes a Rejection, the caller quarantines it, and the
stream keeps moving. One malformed row must not stop a source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..config import KNOWN_UNITS

# Reason codes are a closed vocabulary so operators can group and count them.
MISSING_IDENTIFIER = "missing_identifier"
MISSING_VERSION = "missing_version"
INVALID_EVENT_TIME = "invalid_event_time"
INVALID_AMOUNT = "invalid_amount"
INEXACT_MINOR_UNITS = "inexact_minor_units"
UNKNOWN_STATUS = "unknown_status"
UNKNOWN_UNIT = "unknown_unit"
UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
UNKNOWN_OP = "unknown_op"
MALFORMED_PAYLOAD = "malformed_payload"
MISSING_CURRENCY = "missing_currency"


@dataclass(frozen=True)
class CanonicalEvent:
    source: str
    event_id: str
    seq: int
    case_id: str
    version: int
    op: str                       # upsert | delete
    schema_version: int
    payload_sha256: str
    # A tombstone carries identity and version only, so everything below is
    # optional by construction rather than by accident.
    unit_id: str | None = None
    event_time: datetime | None = None
    amount_minor: int | None = None
    currency: str | None = None
    status: str | None = None
    classification: str | None = None
    contact: str | None = None

    @property
    def is_delete(self) -> bool:
        return self.op == "delete"


@dataclass(frozen=True)
class Rejection:
    source: str
    event_id: str
    seq: int
    reason_codes: tuple[str, ...]
    # Field names and codes only. A rejected value is exactly the value we are
    # least sure is safe to store, so it never leaves this boundary.
    detail: dict[str, Any] = field(default_factory=dict)


class _Errors:
    """Collects every problem in one pass so operators see all of them at once."""

    def __init__(self) -> None:
        self.codes: list[str] = []
        self.fields: dict[str, str] = {}

    def add(self, code: str, field_name: str | None = None) -> None:
        if code not in self.codes:
            self.codes.append(code)
        if field_name:
            self.fields[field_name] = code

    def __bool__(self) -> bool:
        return bool(self.codes)


STATUS_MAPS: dict[str, dict[Any, str]] = {
    "aster": {"open": "open", "completed": "completed", "cancelled": "cancelled"},
    "birch": {"O": "open", "D": "completed", "X": "cancelled"},
    "cobalt": {10: "open", 20: "completed", 90: "cancelled"},
}

IDENTIFIER_KEY = {"aster": "case_id", "birch": "ticket", "cobalt": "ref"}
UNIT_KEY = {"aster": "unit_id", "birch": "branch", "cobalt": "site"}


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash for lineage. sort_keys makes redelivery hash identically."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _to_minor_from_decimal_string(raw: Any, errors: _Errors, field_name: str) -> int | None:
    """Major-unit decimal string to exact minor units.

    Never float: the fixture generator warns about it explicitly, and a binary
    float cannot represent 143.64. A value that is not exactly representable in
    minor units is a validation failure, not a rounding opportunity.
    """
    if not isinstance(raw, (str, int)):
        errors.add(INVALID_AMOUNT, field_name)
        return None
    try:
        scaled = Decimal(str(raw)) * 100
    except (InvalidOperation, ValueError):
        errors.add(INVALID_AMOUNT, field_name)
        return None
    if not scaled.is_finite():
        errors.add(INVALID_AMOUNT, field_name)
        return None
    # Compare against the rounded value rather than relying on to_integral_exact:
    # that method only *signals* Inexact, and the default decimal context has the
    # trap disabled, so it would quietly round 143.6449 to 14364 and lose money.
    integral = scaled.to_integral_value()
    if integral != scaled:
        errors.add(INEXACT_MINOR_UNITS, field_name)
        return None
    return int(integral)


def _to_minor_from_int(raw: Any, errors: _Errors, field_name: str) -> int | None:
    # bool is an int subclass; a boolean amount is malformed, not zero or one.
    if isinstance(raw, bool) or not isinstance(raw, int):
        errors.add(INVALID_AMOUNT, field_name)
        return None
    return raw


def _parse_iso(raw: Any, errors: _Errors, field_name: str) -> datetime | None:
    if not isinstance(raw, str):
        errors.add(INVALID_EVENT_TIME, field_name)
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        errors.add(INVALID_EVENT_TIME, field_name)
        return None
    # A naive timestamp has no defined instant. Treating it as UTC would be a
    # guess that silently shifts a business day, so reject it instead.
    if parsed.tzinfo is None:
        errors.add(INVALID_EVENT_TIME, field_name)
        return None
    return parsed.astimezone(timezone.utc)


def _parse_epoch_ms(raw: Any, errors: _Errors, field_name: str) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        errors.add(INVALID_EVENT_TIME, field_name)
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        errors.add(INVALID_EVENT_TIME, field_name)
        return None


def _aster(payload: dict[str, Any], errors: _Errors) -> dict[str, Any]:
    return {
        "unit_id": payload.get("unit_id"),
        "event_time": _parse_iso(payload.get("event_time"), errors, "event_time"),
        "amount_minor": _to_minor_from_int(payload.get("amount_minor"), errors, "amount_minor"),
        "status_raw": payload.get("status"),
    }


def _birch(payload: dict[str, Any], errors: _Errors) -> dict[str, Any]:
    # v2 moved the monetary field and removed the old one. Read by declared
    # schema_version rather than by "whichever key happens to be present", so a
    # future v3 fails loudly instead of silently picking a stale field.
    schema_version = payload.get("schema_version")
    if schema_version == 2:
        amount_field = "net_amount"
    elif schema_version == 1:
        amount_field = "amount"
    else:
        errors.add(UNSUPPORTED_SCHEMA_VERSION, "schema_version")
        amount_field = "amount"

    return {
        "unit_id": payload.get("branch"),
        "event_time": _parse_iso(payload.get("occurred_at"), errors, "occurred_at"),
        "amount_minor": _to_minor_from_decimal_string(payload.get(amount_field), errors, amount_field),
        "status_raw": payload.get("state"),
    }


def _cobalt(payload: dict[str, Any], errors: _Errors) -> dict[str, Any]:
    return {
        "unit_id": payload.get("site"),
        "event_time": _parse_epoch_ms(payload.get("timestamp_ms"), errors, "timestamp_ms"),
        "amount_minor": _to_minor_from_int(payload.get("value_minor"), errors, "value_minor"),
        "status_raw": payload.get("status_code"),
    }


ADAPTERS: dict[str, Callable[[dict[str, Any], _Errors], dict[str, Any]]] = {
    "aster": _aster,
    "birch": _birch,
    "cobalt": _cobalt,
}


def normalize(source: str, item: dict[str, Any]) -> CanonicalEvent | Rejection:
    """One source envelope to a canonical event, or a rejection describing why."""
    event_id = item.get("event_id")
    seq = item.get("seq")
    if not isinstance(event_id, str) or not event_id or not isinstance(seq, int):
        return Rejection(
            source=source,
            event_id=event_id if isinstance(event_id, str) and event_id else "<unknown>",
            seq=seq if isinstance(seq, int) else -1,
            reason_codes=(MALFORMED_PAYLOAD,),
            detail={"envelope": "missing event_id or seq"},
        )

    payload = item.get("payload")
    if not isinstance(payload, dict):
        return Rejection(source, event_id, seq, (MALFORMED_PAYLOAD,), {"payload": "not an object"})

    errors = _Errors()
    digest = payload_hash(payload)

    op = payload.get("op", "upsert")
    if op not in {"upsert", "delete"}:
        errors.add(UNKNOWN_OP, "op")

    case_id = payload.get(IDENTIFIER_KEY[source])
    if not isinstance(case_id, str) or not case_id:
        errors.add(MISSING_IDENTIFIER, IDENTIFIER_KEY[source])

    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        errors.add(MISSING_VERSION, "version")

    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        errors.add(UNSUPPORTED_SCHEMA_VERSION, "schema_version")

    # A tombstone legitimately omits every business field, so validating them
    # would quarantine correct deletes.
    if op == "delete":
        if errors:
            return Rejection(source, event_id, seq, tuple(errors.codes), errors.fields)
        return CanonicalEvent(
            source=source, event_id=event_id, seq=seq, case_id=case_id,  # type: ignore[arg-type]
            version=version, op="delete", schema_version=schema_version,  # type: ignore[arg-type]
            payload_sha256=digest,
        )

    fields = ADAPTERS[source](payload, errors)

    unit_id = fields["unit_id"]
    if not isinstance(unit_id, str) or unit_id not in KNOWN_UNITS:
        errors.add(UNKNOWN_UNIT, UNIT_KEY[source])
        unit_id = None

    status = STATUS_MAPS[source].get(fields["status_raw"])
    if status is None:
        errors.add(UNKNOWN_STATUS, "status")

    classification = payload.get("classification")
    if classification not in {"internal", "restricted"}:
        # Unknown sensitivity is treated as the stricter of the two rather than
        # rejected, so an unclassified case is still counted but never exposed
        # to an internal-clearance caller.
        classification = "restricted"

    currency = payload.get("currency")
    if not isinstance(currency, str) or not currency:
        errors.add(MISSING_CURRENCY, "currency")

    contact = payload.get("contact")
    if not isinstance(contact, str):
        contact = None

    if errors:
        return Rejection(source, event_id, seq, tuple(errors.codes), errors.fields)

    return CanonicalEvent(
        source=source, event_id=event_id, seq=seq, case_id=case_id,  # type: ignore[arg-type]
        version=version, op="upsert", schema_version=schema_version,  # type: ignore[arg-type]
        payload_sha256=digest, unit_id=unit_id, event_time=fields["event_time"],
        amount_minor=fields["amount_minor"], currency=currency, status=status,
        classification=classification, contact=contact,
    )
