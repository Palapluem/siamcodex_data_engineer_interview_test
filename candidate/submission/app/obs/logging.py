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

# Contacts in this dataset are addresses at *.example.invalid, but a real
# deployment would see ordinary addresses, so match the general shape.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(token|authorization|api[_-]?key|password|secret)\b(\s*[:=]\s*)\S+"),
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),  # bare high-entropy strings
)

_SAFE_LONG = re.compile(r"^[0-9a-f]{40,64}$")  # our own sha256 lineage hashes


def redact(value: str) -> str:
    for pattern in _PATTERNS:
        if pattern.pattern.endswith(r"\b") or "40," in pattern.pattern:
            value = pattern.sub(lambda m: m.group(0) if _SAFE_LONG.match(m.group(0)) else REDACTED, value)
        elif "token|authorization" in pattern.pattern:
            value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", value)
        else:
            value = pattern.sub(REDACTED, value)
    return value


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
