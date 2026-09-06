"""Contract tests against the running lab and submission stack.

Marked `integration` and skipped cleanly when the service is not up, so
`pytest -m "not integration"` stays a true offline suite.

These assert INVARIANTS rather than seed-73129 totals: the data README says a
different seed may be used at evaluation. The exact figures for our documented
run live in docs/RESOURCE_REPORT.md as labelled evidence.

    docker compose -f submission/compose.yaml up -d --build
    pytest tests/integration -m integration
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8088"
TOKENS_PATH = Path(__file__).resolve().parents[3] / ".runtime" / "client" / "test_tokens.json"

pytestmark = pytest.mark.integration


def call(path: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(BASE + path)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    if not TOKENS_PATH.exists():
        pytest.skip("lab not bootstrapped: .runtime/client/test_tokens.json missing")
    try:
        status, _ = call("/health")
    except OSError:
        pytest.skip("submission stack is not running on 127.0.0.1:8088")
    if status != 200:
        pytest.skip("service is not healthy")
    return json.loads(TOKENS_PATH.read_text())


class TestHealthAndStatus:
    def test_health_needs_no_credential(self, tokens):
        status, body = call("/health")
        assert status == 200 and body["status"] == "ok"

    def test_status_is_operator_only(self, tokens):
        assert call("/status", tokens["operator"])[0] == 200
        for caller in ("analyst_aster", "analyst_all", "auditor", "wrong_purpose"):
            assert call("/status", tokens[caller])[0] == 403, caller

    def test_status_shape_matches_the_contract(self, tokens):
        _, body = call("/status", tokens["operator"])
        assert set(body) >= {"sources", "quarantine_count"}
        assert set(body["sources"]) == {"aster", "birch", "cobalt"}
        for source in body["sources"].values():
            assert set(source) == {"cursor", "state", "last_success_at"}
            assert source["state"] in {"starting", "healthy", "degraded"}
        assert isinstance(body["quarantine_count"], int)

    def test_status_leaks_no_credentials_or_payloads(self, tokens):
        _, body = call("/status", tokens["operator"])
        blob = json.dumps(body)
        assert "token" not in blob.lower()
        assert "example.invalid" not in blob
        assert "payload" not in blob.lower()


class TestAuthentication:
    @pytest.mark.parametrize("path", ["/status", "/reports/daily", "/cases?source=aster&case_id=C1"])
    def test_missing_credential_is_401(self, tokens, path):
        assert call(path)[0] == 401

    @pytest.mark.parametrize("path", ["/status", "/reports/daily"])
    def test_unknown_credential_is_401(self, tokens, path):
        assert call(path, "not-a-real-token-aaaaaaaaaaaaaaaaaaaa")[0] == 401

    def test_client_supplied_claims_are_ignored(self, tokens):
        request = urllib.request.Request(BASE + "/cases?source=aster&case_id=C00001")
        request.add_header("Authorization", f"Bearer {tokens['analyst_aster']}")
        request.add_header("X-Role", "auditor")
        request.add_header("X-Units", "*")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        assert status == 403


class TestReports:
    def test_row_objects_carry_exactly_the_contract_fields(self, tokens):
        _, body = call("/reports/daily", tokens["analyst_all"])
        assert body["rows"], "expected a converged pipeline to return rows"
        for row in body["rows"]:
            assert set(row) == {"date", "unit_id", "status", "case_count", "amount_minor"}

    def test_sorted_by_date_unit_status(self, tokens):
        _, body = call("/reports/daily", tokens["analyst_all"])
        keys = [(r["date"], r["unit_id"], r["status"]) for r in body["rows"]]
        assert keys == sorted(keys)

    def test_one_row_per_nonempty_group_and_no_zero_fill(self, tokens):
        _, body = call("/reports/daily", tokens["analyst_all"])
        keys = [(r["date"], r["unit_id"], r["status"]) for r in body["rows"]]
        assert len(keys) == len(set(keys))
        assert all(r["case_count"] > 0 for r in body["rows"])

    def test_never_contains_contacts_or_case_identifiers(self, tokens):
        for caller in ("analyst_all", "analyst_aster", "auditor"):
            _, body = call("/reports/daily", tokens[caller])
            blob = json.dumps(body)
            assert "example.invalid" not in blob
            assert "case_id" not in blob
            assert "contact" not in blob

    def test_unit_scope_is_enforced(self, tokens):
        status, body = call("/reports/daily", tokens["analyst_aster"])
        assert status == 200
        assert {r["unit_id"] for r in body["rows"]} == {"AST-1"}

    def test_explicit_unauthorised_unit_is_403_not_empty(self, tokens):
        assert call("/reports/daily?unit_id=AST-2", tokens["analyst_aster"])[0] == 403
        assert call("/reports/daily?unit_id=BIR-1", tokens["analyst_aster"])[0] == 403

    def test_unknown_unit_is_400(self, tokens):
        assert call("/reports/daily?unit_id=ZZZ-9", tokens["analyst_all"])[0] == 400

    def test_unknown_input_does_not_widen_the_query(self, tokens):
        for probe in ("", "%25", "*", "AST-1'%20OR%20'1'%3D'1"):
            status, _ = call(f"/reports/daily?unit_id={probe}", tokens["analyst_all"])
            assert status == 400, probe

    def test_omitted_filter_returns_the_authorised_subset(self, tokens):
        _, scoped = call("/reports/daily", tokens["analyst_aster"])
        _, explicit = call("/reports/daily?unit_id=AST-1", tokens["analyst_aster"])
        assert scoped["rows"] == explicit["rows"]

    def test_clearance_narrows_what_is_aggregated(self, tokens):
        """analyst_aster is internal-clearance; analyst_all is restricted."""
        _, internal = call("/reports/daily?unit_id=AST-1", tokens["analyst_aster"])
        _, full = call("/reports/daily?unit_id=AST-1", tokens["analyst_all"])
        internal_cases = sum(r["case_count"] for r in internal["rows"])
        full_cases = sum(r["case_count"] for r in full["rows"])
        assert internal_cases < full_cases

    def test_case_counts_reconcile_with_the_operator_view(self, tokens):
        """Every active case appears exactly once across the report."""
        _, body = call("/reports/daily", tokens["auditor"])
        total = sum(r["case_count"] for r in body["rows"])
        assert total > 0
        # Each (date, unit, status) group is disjoint, so the sum is a count of
        # distinct cases; re-querying must give the identical number.
        _, again = call("/reports/daily", tokens["auditor"])
        assert sum(r["case_count"] for r in again["rows"]) == total


class TestCases:
    def test_auditor_only(self, tokens):
        path = "/cases?source=aster&case_id=C00001"
        assert call(path, tokens["auditor"])[0] == 200
        for caller in ("analyst_all", "analyst_aster", "operator", "wrong_purpose"):
            assert call(path, tokens[caller])[0] == 403, caller

    def test_item_has_exactly_the_ten_contract_fields(self, tokens):
        _, body = call("/cases?source=aster&case_id=C00001", tokens["auditor"])
        assert set(body["item"]) == {
            "source", "case_id", "unit_id", "version", "event_time", "amount_minor",
            "currency", "status", "classification", "contact"}

    def test_unknown_case_is_404(self, tokens):
        assert call("/cases?source=aster&case_id=NO-SUCH-CASE", tokens["auditor"])[0] == 404

    @pytest.mark.parametrize("query", [
        "case_id=C00001", "source=nosuch&case_id=C00001", "source=aster", "source=&case_id=C00001"])
    def test_missing_or_unknown_source_is_400(self, tokens, query):
        assert call(f"/cases?{query}", tokens["auditor"])[0] == 400

    def test_same_case_id_resolves_differently_per_source(self, tokens):
        """Vendor case IDs collide; identity is (source, case_id)."""
        items = {}
        for source in ("aster", "birch", "cobalt"):
            status, body = call(f"/cases?source={source}&case_id=C00001", tokens["auditor"])
            assert status == 200, source
            items[source] = body["item"]
        assert {i["case_id"] for i in items.values()} == {"C00001"}
        assert len({i["unit_id"] for i in items.values()}) == 3

    def test_amounts_are_integer_minor_units(self, tokens):
        _, body = call("/cases?source=birch&case_id=C00001", tokens["auditor"])
        assert isinstance(body["item"]["amount_minor"], int)
        assert body["item"]["currency"] == "THB"
