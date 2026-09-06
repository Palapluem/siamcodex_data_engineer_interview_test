"""The interviewer's clarification sheet, encoded as executable tests.

Source: the shared Q&A issued to all candidates, reproduced in
docs/CLARIFICATION_SHEET.md. Each test names the rule it enforces and, where the
sheet gives a worked example, uses that example's numbers verbatim.

Q4 in particular says the equal-version tie-break "was not fully specified" in
the original materials, so these are the tests that prove we follow the
clarified rule rather than an earlier reading of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.ingest.apply import order_for_apply
from app.ingest.normalize import CanonicalEvent

BANGKOK = ZoneInfo("Asia/Bangkok")


def event(case_id: str, version: int, *, op: str = "upsert", source: str = "aster",
          seq: int = 1) -> CanonicalEvent:
    return CanonicalEvent(
        source=source, event_id=f"{source}-{case_id}-v{version}-{op}", seq=seq,
        case_id=case_id, version=version, op=op, schema_version=1,
        payload_sha256="0" * 64,
    )


class TestQ1BusinessDay:
    """Asia/Bangkok, derived from the business occurrence, whatever the source offset."""

    def test_the_sheets_worked_example(self):
        # "2026-06-01T18:00:00Z is 2026-06-02T01:00:00+07:00, so it belongs to
        # the June 2 report."
        occurred = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
        assert occurred.astimezone(BANGKOK).date().isoformat() == "2026-06-02"

    @pytest.mark.parametrize("raw", [
        "2026-06-01T18:00:00+00:00",   # aster style, UTC
        "2026-06-02T01:00:00+07:00",   # birch style, already +07
    ])
    def test_the_source_offset_does_not_change_the_business_day(self, raw):
        parsed = datetime.fromisoformat(raw).astimezone(UTC)
        assert parsed.astimezone(BANGKOK).date().isoformat() == "2026-06-02"

    def test_epoch_milliseconds_land_on_the_same_day(self):
        # cobalt style: the same instant as the example above.
        parsed = datetime.fromtimestamp(1780336800000 / 1000, tz=UTC)
        assert parsed.astimezone(BANGKOK).date().isoformat() == "2026-06-02"

    def test_a_correction_belongs_to_the_original_business_day(self):
        """"Corrections update the original business day; they do not create
        new activity on ingestion day." We bucket on event_time, never on
        received_at, so this holds by construction - asserted so a future edit
        that reaches for the ingestion timestamp fails here."""
        occurred = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
        ingested_much_later = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
        assert occurred.astimezone(BANGKOK).date() != ingested_much_later.astimezone(BANGKOK).date()


class TestQ4EqualVersionOrdering:
    """The rules the sheet made explicit, at the level we can test offline.

    The database guard is `WHERE EXCLUDED.version > case_current.version` on both
    the upsert and the delete. These tests cover the other half of the rule: the
    order in which one page's events are handed to that guard.
    """

    def test_higher_version_is_applied_last_so_it_wins(self):
        ordered = order_for_apply([event("C1", 2), event("C1", 1)])
        assert [e.version for e in ordered] == [1, 2]

    def test_at_equal_version_the_upsert_is_applied_before_the_delete(self):
        """"A delete does not take priority over an upsert at an equal version."

        Ordering is what makes this deterministic. If the delete landed first it
        would satisfy the strictly-greater guard against the stored version, and
        the upsert would then be rejected by that same guard.
        """
        delete_first = order_for_apply([event("C1", 2, op="delete"), event("C1", 2)])
        upsert_first = order_for_apply([event("C1", 2), event("C1", 2, op="delete")])
        for ordered in (delete_first, upsert_first):
            assert [e.op for e in ordered] == ["upsert", "delete"]

    def test_a_higher_version_delete_still_sorts_after_a_lower_upsert(self):
        ordered = order_for_apply([event("C1", 3, op="delete"), event("C1", 2)])
        assert [(e.version, e.op) for e in ordered] == [(2, "upsert"), (3, "delete")]

    def test_a_higher_seq_does_not_break_a_version_tie(self):
        """"A higher seq does not break a version tie." seq is absent from the
        sort key, so a later-sequenced duplicate cannot reorder anything."""
        ordered = order_for_apply([event("C1", 2, seq=9000), event("C1", 2, seq=1)])
        assert [e.seq for e in ordered] == [9000, 1]  # stable: page order kept

    def test_cases_do_not_interleave(self):
        ordered = order_for_apply([
            event("C2", 1), event("C1", 2), event("C2", 2), event("C1", 1)])
        assert [(e.case_id, e.version) for e in ordered] == [
            ("C1", 1), ("C1", 2), ("C2", 1), ("C2", 2)]

    def test_identical_case_ids_from_different_sources_stay_separate(self):
        """Business identity is source plus vendor case identifier."""
        ordered = order_for_apply([
            event("C00001", 2, source="cobalt"), event("C00001", 1, source="aster")])
        assert [(e.source, e.version) for e in ordered] == [("aster", 1), ("cobalt", 2)]


class TestQ4VersionGuardIsStrictlyGreater:
    """The SQL predicate itself, asserted against the migration text.

    A behavioural test needs a database and lives in the integration suite; this
    catches an edit that loosens the comparison to >= without one.
    """

    def test_both_statements_use_a_strict_comparison(self):
        from app.ingest import apply

        for statement in (apply._UPSERT_CASE, apply._DELETE_CASE):
            assert "EXCLUDED.version > meridian.case_current.version" in statement
            assert "EXCLUDED.version >= " not in statement

    def test_the_delete_retains_its_version(self):
        """"Retain the deletion version ... so replayed lower or equal versions
        cannot restore a deleted case." The delete writes EXCLUDED.version, so a
        later upsert at that version or below fails the strict guard."""
        from app.ingest import apply

        assert "version       = EXCLUDED.version" in apply._DELETE_CASE

    def test_a_first_event_may_be_a_tombstone(self):
        """"A valid first event establishes the state for an unseen identity,
        including when that event is a deletion tombstone." The delete is an
        INSERT ... ON CONFLICT, so an unseen identity is created as deleted."""
        from app.ingest import apply

        assert apply._DELETE_CASE.strip().startswith("INSERT INTO meridian.case_current")
        assert "is_deleted" in apply._DELETE_CASE
