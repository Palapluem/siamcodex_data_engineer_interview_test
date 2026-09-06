"""Package the submission as a single ZIP for the Siam Codex form.

The form accepts one file up to 100 MB and it is the primary artifact, so the
archive has to stand alone: a reviewer who never opens GitHub must still be able
to build, run and understand the work.

This refuses to produce an archive that contains credentials. The lab bootstrap
writes real (if local-only) keys into candidate/.runtime, and the exercise README
is explicit that those must not be distributed, so a leak here is a scored
failure rather than an inconvenience.

    python candidate/submission/tools/make_submission_zip.py
    python candidate/submission/tools/make_submission_zip.py --name wisit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MAX_BYTES = 100 * 1024 * 1024

# Anything matching these is never packaged, whatever git says.
EXCLUDE_DIRS = {
    ".git", ".runtime", "__pycache__", ".venv", "venv", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "node_modules", "dist", ".idea", ".vscode",
}
EXCLUDE_NAMES = {"test_tokens.json", "source_credentials.json", "identity_service.key", "ca.srl"}
EXCLUDE_SUFFIXES = {".key", ".pem", ".csr", ".srl", ".pyc", ".log", ".zip"}

# Private working documents. They belong in the repository because they are part
# of doing the work, but they are addressed to me rather than to the reviewer:
# one carries personal contact details and draft form answers, the other is
# interview preparation. Neither belongs in the graded artifact.
EXCLUDE_PRIVATE = {
    "docs/SUBMISSION_FORM_ANSWERS.md": "personal details and draft form answers",
    "docs/REVIEW_PREP.md": "interview preparation notes",
}

# Content patterns that mean a secret escaped into a tracked file.
# The PEM markers are assembled from fragments so this file does not match
# itself; exempting the scanner by filename would leave it unscanned instead.
_BEGIN = rb"-----BEG" rb"IN "
SECRET_PATTERNS = [
    (re.compile(_BEGIN + rb"[A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(_BEGIN + rb"CERTIFICATE-----"), "certificate block"),
    (re.compile(rb"\"token\"\s*:\s*\"[A-Za-z0-9_-]{30,}\""), "token literal"),
    (re.compile(rb"Bearer\s+[A-Za-z0-9_-]{30,}"), "bearer literal"),
]
# Files that legitimately discuss these patterns without containing a secret.
SCAN_SKIP = {".md", ".pdf"}

# A line carrying this marker is exempt. Used only where a token-shaped string
# is the point of the line, such as the redaction tests.
ALLOW_MARKER = b"fake-credential-for-test"


def tracked_files() -> list[Path]:
    """Prefer git's view so .gitignore is honoured; fall back to a walk."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True, check=True, timeout=30,
        ).stdout
        paths = [ROOT / p.decode() for p in out.split(b"\0") if p]
        if paths:
            return paths
    except (subprocess.SubprocessError, OSError):
        print("warning: git unavailable, falling back to a filesystem walk", file=sys.stderr)
    return [p for p in ROOT.rglob("*") if p.is_file()]


def untracked_files() -> list[str]:
    """Files git does not know about and does not ignore.

    Packaging from git means uncommitted work is silently absent from the
    archive. At 22:00 with a deadline that is exactly the kind of quiet failure
    that ships a ZIP missing the newest code, so it is an error, not a note.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True, check=True, timeout=30,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [p.decode() for p in out.split(b"\0") if p]


def excluded(path: Path) -> str | None:
    rel = path.relative_to(ROOT)
    if (private := EXCLUDE_PRIVATE.get(rel.as_posix())) is not None:
        return f"private: {private}"
    for part in rel.parts[:-1]:
        if part in EXCLUDE_DIRS:
            return f"directory {part}/"
    if rel.name in EXCLUDE_NAMES:
        return f"filename {rel.name}"
    if path.suffix in EXCLUDE_SUFFIXES:
        return f"suffix {path.suffix}"
    if rel.name.startswith(".env"):
        return "env file"
    return None


def scan(path: Path) -> list[str]:
    """Report credential-shaped content, line by line.

    Scanning per line rather than per file lets a single line opt out with an
    explicit marker. Tests for the redaction filter necessarily contain
    token-shaped strings; exempting those files wholesale would leave them
    unscanned, whereas a visible per-line marker stays greppable and reviewable.
    """
    if path.suffix in SCAN_SKIP:
        return []
    try:
        blob = path.read_bytes()
    except OSError:
        return []

    findings: list[str] = []
    for line in blob.splitlines():
        if ALLOW_MARKER in line:
            continue
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(line) and label not in findings:
                findings.append(label)
    return findings


def verify_fixtures() -> bool:
    data = ROOT / "candidate" / "data"
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    ok = True
    for source, meta in sorted(manifest["sources"].items()):
        digest = hashlib.sha256((data / f"{source}.jsonl").read_bytes()).hexdigest()
        if digest != meta["sha256"]:
            print(f"  FIXTURE MISMATCH: {source}.jsonl", file=sys.stderr)
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="wisit_suwannao", help="name fragment for the archive")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--allow-secrets", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-untracked", action="store_true",
                        help="package anyway when uncommitted files exist")
    args = parser.parse_args()

    stragglers = [p for p in untracked_files() if excluded(ROOT / p) is None]
    if stragglers:
        print(f"{len(stragglers)} file(s) are not committed and would be MISSING from the archive:",
              file=sys.stderr)
        for path in stragglers[:20]:
            print(f"  {path}", file=sys.stderr)
        if not args.allow_untracked:
            print("\nCommit them (or re-run with --allow-untracked), then package again.", file=sys.stderr)
            return 1

    print("Verifying fixture hashes against manifest...")
    if not verify_fixtures():
        print("Fixtures do not match the manifest. Fix this before packaging.", file=sys.stderr)
        return 1
    print("  all fixtures OK")

    include: list[Path] = []
    skipped: dict[str, int] = {}
    for path in sorted(tracked_files()):
        if not path.is_file():
            continue
        reason = excluded(path)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        include.append(path)

    print(f"\nScanning {len(include)} files for credential material...")
    findings = [(p, hits) for p in include if (hits := scan(p))]
    if findings:
        for path, hits in findings:
            print(f"  {path.relative_to(ROOT)}: {', '.join(hits)}", file=sys.stderr)
        if not args.allow_secrets:
            print("\nRefusing to package. Remove the material above, then re-run.", file=sys.stderr)
            return 1
    else:
        print("  clean")

    stem = f"siamcodex_data_engineer_{args.name}_{date.today():%Y%m%d}"
    args.out.mkdir(parents=True, exist_ok=True)
    archive = args.out / f"{stem}.zip"

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in include:
            zf.write(path, arcname=str(Path(stem) / path.relative_to(ROOT)))

    size = archive.stat().st_size
    print(f"\nWrote {archive}")
    print(f"  {len(include)} files, {size / 1_048_576:.1f} MiB (limit 100 MB)")
    if skipped:
        print("  excluded: " + ", ".join(f"{n}x {r}" for r, n in sorted(skipped.items())))

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()

    leaked = [n for n in names if ".runtime" in n or n.endswith(".key")]
    leaked += [n for n in names
               if any(n.endswith(private) for private in EXCLUDE_PRIVATE)]
    if leaked:
        print(f"\nARCHIVE CONTAINS SECRETS: {leaked[:5]}", file=sys.stderr)
        return 1
    print("  verified: no .runtime, no key files, no private working documents")

    if size > MAX_BYTES:
        over = (size - MAX_BYTES) / 1_048_576
        print(f"\nArchive exceeds the 100 MB form limit by {over:.1f} MiB", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
