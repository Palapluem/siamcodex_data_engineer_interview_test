"""Run the resilience scenarios the interviewer will run, and capture evidence.

Host-side. Each scenario restores the lab to its prior state, so they can run in
any order and repeatedly.

    python submission/tools/scenarios.py            # all
    python submission/tools/scenarios.py outage restart
"""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "submission" / "compose.yaml")]
CONTROL = [sys.executable, str(Path(__file__).with_name("lab_control.py"))]
TOKENS = json.loads((ROOT / ".runtime" / "client" / "test_tokens.json").read_text())
BASE = "http://127.0.0.1:8088"
EVIDENCE = ROOT.parent / "evidence"


def api(path: str, caller: str) -> tuple[int, dict]:
    request = urllib.request.Request(BASE + path)
    request.add_header("Authorization", f"Bearer {TOKENS[caller]}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        # A restarting container refuses or drops the connection; that is a
        # 'not ready' signal for wait_for, not a scenario failure.
        return 0, {}


def sql(query: str) -> str:
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-U", "pipeline", "-d", "meridian", "-tAc", query],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


def control(*args: str) -> None:
    subprocess.run([*CONTROL, *args], capture_output=True, check=True, timeout=30)


def totals() -> dict:
    row = sql("SELECT count(*) FILTER (WHERE NOT is_deleted), count(*) FILTER (WHERE is_deleted), "
              "coalesce(sum(amount_minor) FILTER (WHERE NOT is_deleted),0) FROM meridian.case_current")
    active, deleted, amount = row.split("|")
    return {"active": int(active), "deleted": int(deleted), "amount_minor": int(amount),
            "quarantined": int(sql("SELECT count(*) FROM meridian.quarantine"))}


def status() -> dict:
    return api("/status", "operator")[1]


def wait_for(predicate, timeout: float, interval: float = 2.0) -> tuple[bool, float]:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return True, time.monotonic() - start
        time.sleep(interval)
    return False, time.monotonic() - start


def log(step: str, detail: str = "") -> None:
    print(f"  {step:<44} {detail}")


# --------------------------------------------------------------------------- #

def scenario_outage() -> dict:
    """One link down for 30s must not affect the other two, and must catch up."""
    print("\n== outage: birch down for 30s ==")
    before = totals()
    control("outage", "birch", "on")

    ok, _ = wait_for(lambda: status()["sources"]["birch"]["state"] == "degraded", 40)
    states = {s: v["state"] for s, v in status()["sources"].items()}
    log("birch degraded", f"{ok} -> {states}")
    isolated = states["aster"] == "healthy" and states["cobalt"] == "healthy"
    log("aster and cobalt stayed healthy", str(isolated))

    # Other sources must keep making successful polls during the outage.
    mark = status()["sources"]["aster"]["last_success_at"]
    time.sleep(30)
    moved = status()["sources"]["aster"]["last_success_at"] != mark
    log("aster kept polling through the outage", str(moved))

    control("outage", "birch", "off")
    recovered, seconds = wait_for(
        lambda: status()["sources"]["birch"]["state"] == "healthy", 60)
    log("birch recovered", f"{recovered} in {seconds:.0f}s (target 60s)")

    after = totals()
    log("totals unchanged", f"{before} -> {after}")
    return {"degraded": ok, "isolated": isolated, "others_kept_polling": moved,
            "recovered": recovered, "recovery_seconds": round(seconds, 1),
            "totals_stable": before == after}


def scenario_revocation() -> dict:
    """A revoked token must stop working within the 5s cache TTL."""
    print("\n== revocation: analyst_all ==")
    code, _ = api("/reports/daily", "analyst_all")
    log("before revocation", str(code))

    control("revoke", "analyst_all")
    denied, seconds = wait_for(lambda: api("/reports/daily", "analyst_all")[0] == 401, 15, 0.5)
    log("denied after revocation", f"{denied} in {seconds:.1f}s (cache TTL 5s)")

    others = api("/reports/daily", "analyst_aster")[0]
    log("other callers unaffected", str(others))

    control("unrevoke", "analyst_all")
    restored, _ = wait_for(lambda: api("/reports/daily", "analyst_all")[0] == 200, 15, 0.5)
    log("restored after un-revocation", str(restored))
    return {"before": code, "denied": denied, "denied_after_seconds": round(seconds, 1),
            "within_ttl": seconds <= 6.0, "others_unaffected": others == 200, "restored": restored}


def scenario_restart() -> dict:
    """A restart with volumes retained must preserve every number."""
    print("\n== restart: pipeline container, volumes retained ==")
    before = totals()
    cursors_before = {s: v["cursor"] for s, v in status()["sources"].items()}

    subprocess.run([*COMPOSE, "restart", "pipeline"], capture_output=True, check=True, timeout=120)
    back, seconds = wait_for(lambda: api("/status", "operator")[0] == 200, 90, 1.0)
    log("service back", f"{back} in {seconds:.0f}s")

    healthy, _ = wait_for(
        lambda: all(v["state"] == "healthy" for v in status()["sources"].values()), 60)
    after = totals()
    cursors_after = {s: v["cursor"] for s, v in status()["sources"].items()}

    log("totals identical", f"{before == after}  {after}")
    log("cursors resumed, not reset", f"{cursors_before} -> {cursors_after}")
    return {"back": back, "restart_seconds": round(seconds, 1), "healthy_again": healthy,
            "totals_identical": before == after, "cursors_resumed": cursors_after == cursors_before}


def scenario_replay() -> dict:
    """Replaying every source from cursor 0 must change nothing."""
    print("\n== replay: reset all cursors to 0 ==")
    before = totals()
    seen_before = int(sql("SELECT count(*) FROM meridian.event_seen"))

    sql("UPDATE meridian.source_state SET cursor='0'")
    log("cursors reset", "0/0/0")

    caught_up, seconds = wait_for(
        lambda: sql("SELECT min(cursor::bigint) FROM meridian.source_state") not in ("", "0")
        and int(sql("SELECT min(cursor::bigint) FROM meridian.source_state")) >= 3333, 180, 3.0)
    after = totals()
    seen_after = int(sql("SELECT count(*) FROM meridian.event_seen"))

    log("re-ingested", f"{caught_up} in {seconds:.0f}s")
    log("business totals unchanged", f"{before == after}  {after}")
    log("quarantine not inflated", f"{before['quarantined']} -> {after['quarantined']}")
    log("event_seen not duplicated", f"{seen_before} -> {seen_after}")
    return {"caught_up": caught_up, "replay_seconds": round(seconds, 1),
            "totals_identical": before == after,
            "quarantine_stable": before["quarantined"] == after["quarantined"],
            "event_seen_stable": seen_before == seen_after}


def scenario_identity_down() -> dict:
    """With identity unreachable, business routes fail closed but liveness holds."""
    print("\n== identity outage: fail closed ==")
    subprocess.run(["docker", "compose", "-f", str(ROOT / "infrastructure" / "compose.yaml"),
                    "stop", "identity"], capture_output=True, check=True, timeout=60)
    time.sleep(7)  # outlast the 5s introspection cache

    reports = api("/reports/daily", "analyst_all")[0]
    health = urllib.request.urlopen(BASE + "/health", timeout=10).status
    log("business route fails closed", f"{reports} (want 503, never 200)")
    log("liveness unaffected", f"{health} (want 200)")

    subprocess.run(["docker", "compose", "-f", str(ROOT / "infrastructure" / "compose.yaml"),
                    "start", "identity"], capture_output=True, check=True, timeout=120)
    restored, seconds = wait_for(lambda: api("/reports/daily", "analyst_all")[0] == 200, 60, 1.0)
    log("restored", f"{restored} in {seconds:.0f}s")
    return {"business_status": reports, "failed_closed": reports in (503,),
            "health_status": health, "restored": restored}


SCENARIOS = {
    "outage": scenario_outage,
    "revocation": scenario_revocation,
    "restart": scenario_restart,
    "replay": scenario_replay,
    "identity": scenario_identity_down,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", choices=list(SCENARIOS), default=None,
                        help="scenarios to run (default: all)")
    args = parser.parse_args()
    names = args.names or list(SCENARIOS)

    results = {"started_at": datetime.now(UTC).isoformat(), "scenarios": {}}
    for name in names:
        results["scenarios"][name] = SCENARIOS[name]()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / "scenarios.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nEvidence written to {out}")

    failed = [n for n, r in results["scenarios"].items()
              if not all(v for v in r.values() if isinstance(v, bool))]
    if failed:
        print(f"SCENARIOS WITH FAILED ASSERTIONS: {failed}")
        return 1
    print("All scenario assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
