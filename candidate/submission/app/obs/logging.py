"""Structured JSON logging with unconditional redaction.

The contract forbids logging tokens or contacts and requires redacting sensitive
values in error messages too. Discipline at each call site is not enough: one
interpolated exception from a library is all it takes. So redaction runs as a
filter over every record, including messages this code did not write.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

REDACTED = "[redacted]"

# Our own sha256 lineage hashes are not secrets, and losing them would make the
# lineage log useless, so they are exempted from the high-entropy rule.
_SAFE_LONG = re.compile(r"\A[0-9a-f]{40,64}\Z")

# Contacts in this dataset are addresses at *.example.invalid, but a real
# deployment would see ordinary addresses, so match the general shape.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A labelled secret, in prose (token=abc), JSON ("api_key": "abc") or a header.
# The optional quotes are what makes the JSON form match.
_LABELLED = re.compile(
    r"""(?ix)
    \b(token|authorization|api[_-]?key|password|passwd|secret|credential)\b
    ["']? \s* [:=] \s* ["']?
    ([^\s"',;}\]]+)
    """
)

_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

# Anything long and random-looking that no rule above claimed.
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")


def _mask_labelled(match: re.Match[str]) -> str:
    return f"{match.group(1)}={REDACTED}"


def _mask_high_entropy(match: re.Match[str]) -> str:
    return match.group(0) if _SAFE_LONG.match(match.group(0)) else REDACTED


def redact(value: str) -> str:
    """Strip credential and contact material from arbitrary text.

    Ordered from most specific to least: a labelled secret keeps its label so
    the log still says *what* was withheld, and the catch-all entropy rule runs
    last so it only sees what nothing else claimed.
    """
    value = _EMAIL.sub(REDACTED, value)
    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _LABELLED.sub(_mask_labelled, value)
    return _HIGH_ENTROPY.sub(_mask_high_entropy, value)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    _BUILTIN = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
        "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN and not key.startswith("_"):
                payload[key] = _scrub(value)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers; route them through ours so nothing
    # escapes redaction.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
