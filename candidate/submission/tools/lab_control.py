"""Drive the fixed lab's scenario switches for our own testing.

Host-side test harness, not part of the service. The interviewer flips these
same switches during evaluation; we exercise them first so nothing is a surprise.

The fixture re-reads control.json on every request with a bounded retry and the
file is padded to a fixed length, so writes must be atomic and re-padded -
a torn read is exactly the flakiness that padding exists to avoid.

    python submission/tools/lab_control.py show
    python submission/tools/lab_control.py phase 2
    python submission/tools/lab_control.py outage birch on
    python submission/tools/lab_control.py revoke analyst_all
    python submission/tools/lab_control.py reset
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[2] / ".runtime"
CONTROL = RUNTIME / "server" / "control" / "control.json"
TOKENS = RUNTIME / "client" / "test_tokens.json"
PAD = 4096

DEFAULT = {"phase": 1, "outages": {}, "revoked_tokens": [], "transient_errors": True}


def read() -> dict:
    return json.loads(CONTROL.read_text(encoding="utf-8"))


def write(state: dict) -> None:
    body = json.dumps(state).ljust(PAD)
    if len(body) > PAD:
        raise SystemExit("control state exceeds the fixture's 4096-byte window")
    # Write-then-rename so a reader never sees a half-written file. The fixture
    # container reads through a bind mount, so the rename must land in the same
    # directory to be atomic.
    fd, tmp = tempfile.mkstemp(dir=str(CONTROL.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CONTROL)
        CONTROL.chmod(0o644)  # the fixture runs as uid 65532 and must still read it
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def show(state: dict) -> None:
    print(json.dumps(state, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("reset")

    phase = sub.add_parser("phase")
    phase.add_argument("value", type=int, choices=[1, 2])

    outage = sub.add_parser("outage")
    outage.add_argument("source", choices=["aster", "birch", "cobalt"])
    outage.add_argument("mode", choices=["on", "off"])

    revoke = sub.add_parser("revoke")
    revoke.add_argument("caller")

    unrevoke = sub.add_parser("unrevoke")
    unrevoke.add_argument("caller")

    transient = sub.add_parser("transient")
    transient.add_argument("mode", choices=["on", "off"])

    args = parser.parse_args()
    state = read()

    if args.command == "show":
        show(state)
        return 0

    if args.command == "reset":
        state = dict(DEFAULT)
    elif args.command == "phase":
        state["phase"] = args.value
    elif args.command == "outage":
        state.setdefault("outages", {})[args.source] = args.mode == "on"
    elif args.command == "transient":
        state["transient_errors"] = args.mode == "on"
    elif args.command in {"revoke", "unrevoke"}:
        tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
        if args.caller not in tokens:
            print(f"unknown caller {args.caller!r}; known: {sorted(tokens)}", file=sys.stderr)
            return 1
        token = tokens[args.caller]
        revoked = list(state.get("revoked_tokens", []))
        if args.command == "revoke":
            if token not in revoked:
                revoked.append(token)
        else:
            revoked = [t for t in revoked if t != token]
        state["revoked_tokens"] = revoked

    write(state)
    # Print the caller-facing view without echoing token material.
    redacted = dict(state, revoked_tokens=[f"<{len(state.get('revoked_tokens', []))} revoked>"])
    show(redacted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
