"""The access policy, tested as a table.

These mirror the five caller fixtures the interviewer uses, so a policy change
that breaks one of them fails here before it reaches the review.
"""

from __future__ import annotations

import pytest

from app.api.auth import Principal, Unauthenticated, authorize, extract_bearer
from app.config import KNOWN_UNITS

ANALYST_ASTER = Principal("u-aster", "analyst", ("AST-1",), "internal", "operations")
ANALYST_ALL = Principal("u-all", "analyst", ("*",), "restricted", "operations")
AUDITOR = Principal("u-audit", "auditor", ("*",), "restricted", "audit")
OPERATOR = Principal("u-operator", "operator", (), "internal", "operations")
WRONG_PURPOSE = Principal("u-purpose", "analyst", ("*",), "restricted", "marketing")


class TestRoutePolicy:
    @pytest.mark.parametrize("principal,route,allowed,reason", [
        # /status is operational visibility, not business access.
        (OPERATOR, "status", True, None),
        (ANALYST_ASTER, "status", False, "role"),
        (ANALYST_ALL, "status", False, "role"),
        (AUDITOR, "status", False, "role"),
        (WRONG_PURPOSE, "status", False, "role"),

        # An operator who restarts jobs must not gain business data.
        (OPERATOR, "reports", False, "role"),
        (OPERATOR, "cases", False, "role"),

        (ANALYST_ASTER, "reports", True, None),
        (ANALYST_ALL, "reports", True, None),
        (AUDITOR, "reports", True, None),

        # Auditors are the only role with case detail, and so with contacts.
        (ANALYST_ASTER, "cases", False, "role"),
        (ANALYST_ALL, "cases", False, "role"),
        (AUDITOR, "cases", True, None),

        # purpose=marketing is denied even with full role and unit scope.
        (WRONG_PURPOSE, "reports", False, "purpose"),
        (WRONG_PURPOSE, "cases", False, "role"),
    ])
    def test_matrix(self, principal, route, allowed, reason):
        decision = authorize(principal, route)
        assert decision.allowed is allowed
        assert decision.reason == reason
        assert decision.status == (200 if allowed else 403)

    def test_unknown_route_denies_rather_than_defaulting_open(self):
        assert authorize(AUDITOR, "some_new_route").allowed is False


class TestUnitScoping:
    def test_wildcard_expands_to_known_units_only(self):
        assert set(ANALYST_ALL.permitted_units(KNOWN_UNITS)) == set(KNOWN_UNITS)

    def test_named_unit_scope(self):
        assert ANALYST_ASTER.permitted_units(KNOWN_UNITS) == ["AST-1"]

    def test_operator_has_no_units(self):
        assert OPERATOR.permitted_units(KNOWN_UNITS) == []

    @pytest.mark.parametrize("unit,expected", [
        ("AST-1", True), ("AST-2", False), ("BIR-1", False), ("COB-2", False)])
    def test_may_see_unit(self, unit, expected):
        assert ANALYST_ASTER.may_see_unit(unit) is expected

    def test_a_scope_naming_an_unknown_unit_cannot_widen_the_query(self):
        rogue = Principal("u-x", "analyst", ("AST-1", "ZZZ-9"), "internal", "operations")
        assert rogue.permitted_units(KNOWN_UNITS) == ["AST-1"]


class TestClearance:
    def test_internal_clearance_does_not_see_restricted(self):
        assert ANALYST_ASTER.sees_restricted is False

    def test_restricted_clearance_sees_everything(self):
        assert ANALYST_ALL.sees_restricted is True
        assert AUDITOR.sees_restricted is True


class TestBearerParsing:
    def test_valid(self):
        assert extract_bearer("Bearer abc123") == "abc123"

    def test_scheme_is_case_insensitive(self):
        assert extract_bearer("bearer abc123") == "abc123"

    @pytest.mark.parametrize("header", [None, "", "abc123", "Basic abc123", "Bearer", "Bearer   "])
    def test_rejected(self, header):
        with pytest.raises(Unauthenticated):
            extract_bearer(header)
