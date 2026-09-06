"""Exercise the full authorisation matrix against the running service.

Host-side only. It reads .runtime/client/test_tokens.json, which belongs to API
callers and is deliberately never mounted into the service.

    python submission/tools/authz_matrix.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8088"
TOKENS = Path(__file__).resolve().parents[2] / ".runtime" / "client" / "test_tokens.json"

# (label, method path, expected status) per caller. These are the cells the
# contract fixes plus the ones our clarification-sheet defaults decide.
EXPECTED: list[tuple[str | None, str, int, str]] = [
    # caller,          path,                              expected, why
    (None, "/health", 200, "liveness is unauthenticated"),
    (None, "/status", 401, "no credential"),
    (None, "/reports/daily", 401, "no credential"),
    (None, "/cases?source=aster&case_id=C00001", 401, "no credential"),
    ("bogus", "/reports/daily", 401, "unknown token is inactive"),

    ("operator", "/status", 200, "status is operator-only"),
    ("operator", "/reports/daily", 403, "operators get no business data"),
    ("operator", "/cases?source=aster&case_id=C00001", 403, "operators get no case detail"),

    ("analyst_aster", "/status", 403, "analysts are not operators"),
    ("analyst_aster", "/reports/daily", 200, "authorised subset"),
    ("analyst_aster", "/reports/daily?unit_id=AST-1", 200, "own unit"),
    ("analyst_aster", "/reports/daily?unit_id=AST-2", 403, "known unit, not permitted"),
    ("analyst_aster", "/reports/daily?unit_id=BIR-1", 403, "another source's unit"),
    ("analyst_aster", "/reports/daily?unit_id=ZZZ-9", 400, "unknown unit is a bad request"),
    ("analyst_aster", "/cases?source=aster&case_id=C00001", 403, "non-auditor"),

    ("analyst_all", "/status", 403, "analysts are not operators"),
    ("analyst_all", "/reports/daily", 200, "all units"),
    ("analyst_all", "/reports/daily?unit_id=COB-2", 200, "wildcard covers every unit"),
    ("analyst_all", "/cases?source=aster&case_id=C00001", 403, "non-auditor"),

    ("auditor", "/status", 403, "auditors are not operators"),
    ("auditor", "/reports/daily", 200, "audit is an allowed purpose"),
    ("auditor", "/cases?source=aster&case_id=C00001", 200, "auditors may inspect a case"),
    ("auditor", "/cases?source=aster&case_id=NOPE", 404, "unknown case"),
    ("auditor", "/cases?source=nosuch&case_id=C00001", 400, "unknown source"),
    ("auditor", "/cases?case_id=C00001", 400, "missing source"),

    ("wrong_purpose", "/status", 403, "not an operator"),
    ("wrong_purpose", "/reports/daily", 403, "purpose=marketing is denied"),
    ("wrong_purpose", "/cases?source=aster&case_id=C00001", 403, "purpose and role both deny"),
]


def call(path: str, token: str | None) -> tuple[int, dict]:
    request = urllib.request.Request(BASE + path)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def main() -> int:
    tokens = json.loads(TOKENS.read_text())
    tokens["bogus"] = "not-a-real-token-000000000000000000"

    failures = 0
    for caller, path, expected, why in EXPECTED:
        token = tokens.get(caller) if caller else None
        status, _ = call(path, token)
        ok = status == expected
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        label = caller or "anonymous"
        print(f"{mark} {label:14} {path:42} -> {status:3} (want {expected})  {why}")

    print()
    print("Header-injection check: client-supplied claims must be ignored")
    for header, value in (("X-Role", "auditor"), ("X-Units", "*"), ("X-Clearance", "restricted")):
        request = urllib.request.Request(BASE + "/cases?source=aster&case_id=C00001")
        request.add_header("Authorization", f"Bearer {tokens['analyst_aster']}")
        request.add_header(header, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        ok = status == 403
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} analyst_aster + {header}: {value:12} -> {status} (want 403)")

    print()
    if failures:
        print(f"{failures} matrix cell(s) FAILED")
        return 1
    print(f"All {len(EXPECTED) + 3} authorisation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
