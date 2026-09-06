"""Runtime configuration, entirely from the environment.

Every contested business semantic is a setting rather than a constant, because
the interviewer supplies a clarification sheet after implementation and may
introduce a bounded change during review. A different answer should cost a
restart, not a rewrite. See docs/ASSUMPTIONS.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

# Fixed by the technical contract. An allowlist is what stops an unknown input
# from turning into an unfiltered query.
KNOWN_UNITS: frozenset[str] = frozenset({"AST-1", "AST-2", "BIR-1", "BIR-2", "COB-1", "COB-2"})
SOURCES: tuple[str, ...] = ("aster", "birch", "cobalt")

# Published source ceilings are 8/4/2 per second. We stay below them: a 429 we
# caused ourselves is wasted latency, and the ceiling is source-wide rather than
# per-connection, so there is nothing to gain by crowding it.
RATE_BUDGET: dict[str, float] = {"aster": 6.0, "birch": 3.0, "cobalt": 1.5}


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    lab_ca_path: Path
    source_credentials_path: Path
    identity_service_key_path: Path
    identity_url: str

    business_timezone: ZoneInfo
    clearance_filters_aggregates: bool

    page_limit: int
    poll_idle_seconds: float
    introspect_cache_seconds: float

    role: str
    log_level: str

    known_units: frozenset[str] = KNOWN_UNITS
    sources: tuple[str, ...] = SOURCES
    rate_budget: dict[str, float] = field(default_factory=lambda: dict(RATE_BUDGET))

    # Connect fast, read patiently: the gateway's own upstream timeout is 5s, so
    # a shorter read timeout would turn its 503 into our timeout and lose the
    # Retry-After hint.
    connect_timeout: float = 3.0
    read_timeout: float = 10.0

    @property
    def runs_api(self) -> bool:
        return self.role in {"all", "api"}

    @property
    def runs_worker(self) -> bool:
        return self.role in {"all", "worker"}


def load_settings() -> Settings:
    role = os.environ.get("ROLE", "all").strip().lower()
    if role not in {"all", "api", "worker"}:
        raise ValueError(f"ROLE must be all, api or worker; got {role!r}")

    page_limit = int(os.environ.get("PAGE_LIMIT", "200"))
    if not 1 <= page_limit <= 200:
        raise ValueError(f"PAGE_LIMIT must be 1-200 (contract); got {page_limit}")

    cache_seconds = float(os.environ.get("INTROSPECT_CACHE_SECONDS", "5"))
    if cache_seconds > 5:
        raise ValueError("INTROSPECT_CACHE_SECONDS may not exceed 5 (contract)")

    return Settings(
        database_url=os.environ["DATABASE_URL"],
        lab_ca_path=Path(os.environ.get("LAB_CA_PATH", "/run/secrets/lab_ca.crt")),
        source_credentials_path=Path(
            os.environ.get("SOURCE_CREDENTIALS_PATH", "/run/secrets/source_credentials.json")
        ),
        identity_service_key_path=Path(
            os.environ.get("IDENTITY_SERVICE_KEY_PATH", "/run/secrets/identity_service.key")
        ),
        identity_url=os.environ.get("IDENTITY_URL", "https://identity:8443/introspect"),
        business_timezone=ZoneInfo(os.environ.get("BUSINESS_TIMEZONE", "Asia/Bangkok")),
        clearance_filters_aggregates=_flag("CLEARANCE_FILTERS_AGGREGATES", True),
        page_limit=page_limit,
        poll_idle_seconds=float(os.environ.get("POLL_IDLE_SECONDS", "3")),
        introspect_cache_seconds=cache_seconds,
        role=role,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
