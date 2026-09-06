"""Verify the fixed exercise fixtures are byte-identical to their manifest.

This is host-side tooling. The running service must never read these files.

A default Windows clone with core.autocrlf=true rewrites every line ending to
CRLF, which changes all three SHA-256 values and breaks the file-hash check the
interviewer runs. Run this after any clone.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"


def main() -> int:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    failures = 0

    for source, meta in sorted(manifest["sources"].items()):
        raw = (DATA / f"{source}.jsonl").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        ok = digest == meta["sha256"]
        failures += not ok
        crlf = raw.count(b"\r\n")
        note = f"  <-- {crlf} CRLF line endings; run: git config core.autocrlf false" if crlf else ""
        print(f"{source:8} {'OK' if ok else 'MISMATCH':9} {digest[:16]}...  events={meta['events']}{note}")

    total = sum(m["events"] for m in manifest["sources"].values())
    print(f"\nseed={manifest['seed']}  total_events={total} (expected {manifest['total_events']})")

    if failures:
        print(f"\n{failures} fixture(s) do not match the manifest. Do not submit until this is clean.")
        return 1
    print("\nAll fixtures match the manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
