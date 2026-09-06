"""Normalisation is where money and identity are decided, so it is tested hardest.

Payload shapes here are taken from real gateway responses (see
tools/peek_source.py output recorded in docs/CONTRACT_FACTS.md), not invented.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ingest import normalize as N
from app.ingest.normalize import CanonicalEvent, Rejection, normalize


def envelope(payload: dict, event_id: str = "e1", seq: int = 1) -> dict:
    return {"event_id": event_id, "seq": seq, "payload": payload}


ASTER_V1 = {
    "amount_minor": 148598, "case_id": "C00001", "classification": "restricted",
    "contact": "person00000@aster.example.invalid", "currency": "THB",
    "event_time": "2026-06-30T15:43:00+00:00", "op": "upsert", "schema_version": 1,
    "status": "completed", "unit_id": "AST-1", "version": 1,
}
BIRCH_V1 = {
    "amount": "143.64", "branch": "BIR-1", "classification": "restricted",
    "contact": "person00000@birch.example.invalid", "currency": "THB",
    "occurred_at": "2026-06-25T02:57:00+07:00", "op": "upsert", "schema_version": 1,
    "state": "D", "ticket": "C00001", "version": 1,
}
COBALT_V1 = {
    "classification": "restricted", "contact": "person00000@cobalt.example.invalid",
    "currency": "THB", "op": "upsert", "ref": "C00001", "schema_version": 1,
    "site": "COB-1", "status_code": 10, "timestamp_ms": 1780449960000,
    "value_minor": 40985, "version": 1,
}


class TestVendorMapping:
    def test_aster(self):
        event = normalize("aster", envelope(ASTER_V1))
        assert isinstance(event, CanonicalEvent)
        assert (event.case_id, event.unit_id, event.status) == ("C00001", "AST-1", "completed")
        assert event.amount_minor == 148598
        assert event.event_time == datetime(2026, 6, 30, 15, 43, tzinfo=timezone.utc)

    def test_birch_decimal_string_becomes_exact_minor_units(self):
        event = normalize("birch", envelope(BIRCH_V1))
        assert isinstance(event, CanonicalEvent)
        # 143.64 THB is not representable as a binary float; this must be exact.
        assert event.amount_minor == 14364
        assert event.status == "completed"  # D
        assert event.case_id == "C00001"

    def test_birch_offset_is_normalised_to_utc(self):
        event = normalize("birch", envelope(BIRCH_V1))
        assert event.event_time == datetime(2026, 6, 24, 19, 57, tzinfo=timezone.utc)

    def test_cobalt_epoch_ms(self):
        event = normalize("cobalt", envelope(COBALT_V1))
        assert isinstance(event, CanonicalEvent)
        assert event.amount_minor == 40985
        assert event.status == "open"  # 10
        assert event.event_time == datetime(2026, 6, 3, 1, 26, tzinfo=timezone.utc)

    @pytest.mark.parametrize("raw,expected", [("O", "open"), ("D", "completed"), ("X", "cancelled")])
    def test_birch_status_vocabulary(self, raw, expected):
        assert normalize("birch", envelope({**BIRCH_V1, "state": raw})).status == expected

    @pytest.mark.parametrize("raw,expected", [(10, "open"), (20, "completed"), (90, "cancelled")])
    def test_cobalt_status_vocabulary(self, raw, expected):
        assert normalize("cobalt", envelope({**COBALT_V1, "status_code": raw})).status == expected

    def test_same_case_id_across_vendors_stays_distinct_by_source(self):
        events = [normalize(s, envelope(p)) for s, p in
                  (("aster", ASTER_V1), ("birch", BIRCH_V1), ("cobalt", COBALT_V1))]
        assert {e.case_id for e in events} == {"C00001"}
        assert len({(e.source, e.case_id) for e in events}) == 3


class TestSchemaEvolution:
    def test_birch_v2_reads_net_amount(self):
        payload = {k: v for k, v in BIRCH_V1.items() if k != "amount"}
        payload |= {"net_amount": "156.14", "state": "D", "schema_version": 2, "version": 2}
        event = normalize("birch", envelope(payload))
        assert isinstance(event, CanonicalEvent)
        assert event.amount_minor == 15614
        assert event.version == 2

    def test_unknown_schema_version_is_quarantined_not_guessed(self):
        payload = {**BIRCH_V1, "schema_version": 3}
        result = normalize("birch", envelope(payload))
        assert isinstance(result, Rejection)
        assert N.UNSUPPORTED_SCHEMA_VERSION in result.reason_codes

    def test_v2_payload_without_net_amount_fails_rather_than_falling_back(self):
        payload = {**BIRCH_V1, "schema_version": 2}  # still has v1 "amount"
        result = normalize("birch", envelope(payload))
        assert isinstance(result, Rejection)
        assert N.INVALID_AMOUNT in result.reason_codes


class TestTombstones:
    @pytest.mark.parametrize("source,key", [("aster", "case_id"), ("birch", "ticket"), ("cobalt", "ref")])
    def test_delete_carries_identity_and_version_only(self, source, key):
        event = normalize(source, envelope(
            {"op": "delete", key: "C00251", "version": 2, "schema_version": 1}))
        assert isinstance(event, CanonicalEvent)
        assert event.is_delete and event.case_id == "C00251" and event.version == 2
        assert event.unit_id is None and event.amount_minor is None and event.status is None

    def test_delete_missing_identifier_is_rejected(self):
        result = normalize("birch", envelope({"op": "delete", "version": 2, "schema_version": 1}))
        assert isinstance(result, Rejection)
        assert N.MISSING_IDENTIFIER in result.reason_codes


class TestQuarantine:
    """The fixture ships 100 deliberately invalid events; these are their shapes."""

    INVALID = {"op": "upsert", "version": 1, "schema_version": 1,
               "case_id": "INVALID-0", "amount_minor": "broken", "event_time": "not-a-time"}

    def test_aster_invalid_collects_every_problem_at_once(self):
        result = normalize("aster", envelope(self.INVALID))
        assert isinstance(result, Rejection)
        assert N.INVALID_AMOUNT in result.reason_codes
        assert N.INVALID_EVENT_TIME in result.reason_codes
        assert N.UNKNOWN_UNIT in result.reason_codes

    @pytest.mark.parametrize("source", ["birch", "cobalt"])
    def test_invalid_uses_asters_key_so_others_lack_an_identifier(self, source):
        result = normalize(source, envelope(self.INVALID))
        assert isinstance(result, Rejection)
        assert N.MISSING_IDENTIFIER in result.reason_codes

    def test_rejection_detail_never_carries_values(self):
        result = normalize("aster", envelope({**self.INVALID, "contact": "leak@example.invalid"}))
        assert isinstance(result, Rejection)
        blob = repr(result)
        assert "leak@example.invalid" not in blob
        assert "broken" not in blob
        assert "not-a-time" not in blob

    def test_unknown_unit_is_rejected(self):
        result = normalize("aster", envelope({**ASTER_V1, "unit_id": "ZZZ-9"}))
        assert isinstance(result, Rejection)
        assert N.UNKNOWN_UNIT in result.reason_codes

    def test_naive_timestamp_is_rejected_rather_than_assumed_utc(self):
        result = normalize("aster", envelope({**ASTER_V1, "event_time": "2026-06-30T15:43:00"}))
        assert isinstance(result, Rejection)
        assert N.INVALID_EVENT_TIME in result.reason_codes

    def test_float_money_that_is_not_exact_minor_units_is_rejected(self):
        result = normalize("birch", envelope({**BIRCH_V1, "amount": "143.6449"}))
        assert isinstance(result, Rejection)
        assert N.INEXACT_MINOR_UNITS in result.reason_codes

    def test_boolean_amount_is_not_silently_an_integer(self):
        result = normalize("aster", envelope({**ASTER_V1, "amount_minor": True}))
        assert isinstance(result, Rejection)
        assert N.INVALID_AMOUNT in result.reason_codes

    def test_missing_classification_defaults_to_the_stricter_value(self):
        payload = {k: v for k, v in ASTER_V1.items() if k != "classification"}
        event = normalize("aster", envelope(payload))
        assert isinstance(event, CanonicalEvent)
        assert event.classification == "restricted"

    def test_malformed_envelope(self):
        result = normalize("aster", {"event_id": None, "seq": "x", "payload": {}})
        assert isinstance(result, Rejection)
        assert N.MALFORMED_PAYLOAD in result.reason_codes


class TestLineageHash:
    def test_redelivery_of_the_same_payload_hashes_identically(self):
        a = normalize("aster", envelope(ASTER_V1, event_id="aster-e00001", seq=1))
        b = normalize("aster", envelope(dict(reversed(list(ASTER_V1.items()))),
                                        event_id="aster-e00001", seq=3001))
        assert a.payload_sha256 == b.payload_sha256

    def test_a_corrected_payload_hashes_differently(self):
        a = normalize("aster", envelope(ASTER_V1))
        b = normalize("aster", envelope({**ASTER_V1, "amount_minor": 149848, "version": 2}))
        assert a.payload_sha256 != b.payload_sha256
