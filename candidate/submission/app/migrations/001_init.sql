-- Meridian operational view: initial schema.
-- Applied at startup and safe to re-run; see app/db.py.

CREATE SCHEMA IF NOT EXISTS meridian;
SET search_path TO meridian;

-- Per-source ingestion state. The cursor lives here rather than in memory so a
-- restart resumes exactly where the last committed page ended.
CREATE TABLE IF NOT EXISTS source_state (
    source               text PRIMARY KEY,
    cursor               text        NOT NULL DEFAULT '0',
    state                text        NOT NULL DEFAULT 'starting'
                                     CHECK (state IN ('starting', 'healthy', 'degraded')),
    last_success_at      timestamptz,
    last_attempt_at      timestamptz,
    last_error_kind      text,
    consecutive_failures integer     NOT NULL DEFAULT 0,
    events_ingested      bigint      NOT NULL DEFAULT 0,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- Idempotency ledger. The same event_id can be delivered again at another seq,
-- so the primary key is what makes a replay a no-op.
CREATE TABLE IF NOT EXISTS event_seen (
    source        text   NOT NULL,
    event_id      text   NOT NULL,
    first_seq     bigint NOT NULL,
    last_seq      bigint NOT NULL,
    deliveries    integer NOT NULL DEFAULT 1,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, event_id)
);

-- Current business truth. Identity is (source, case_id): vendor case IDs
-- deliberately collide, so case_id alone is not a key.
CREATE TABLE IF NOT EXISTS case_current (
    source         text    NOT NULL,
    case_id        text    NOT NULL,
    unit_id        text,
    version        integer NOT NULL,
    event_time     timestamptz,
    amount_minor   bigint,
    currency       text,
    status         text CHECK (status IS NULL OR status IN ('open', 'completed', 'cancelled')),
    classification text CHECK (classification IS NULL OR classification IN ('internal', 'restricted')),
    contact        text,
    is_deleted     boolean NOT NULL DEFAULT false,
    last_event_id  text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, case_id)
);

-- Reports read only live rows; a partial index keeps the scan to those.
CREATE INDEX IF NOT EXISTS case_current_report_idx
    ON case_current (unit_id, status) WHERE NOT is_deleted;

-- Append-only lineage so the assurance team can trace a case to the delivery
-- that produced it. The payload hash proves provenance without making a second
-- copy of restricted data.
CREATE TABLE IF NOT EXISTS case_event (
    source         text   NOT NULL,
    event_id       text   NOT NULL,
    seq            bigint NOT NULL,
    case_id        text,
    version        integer,
    op             text,
    schema_version integer,
    payload_sha256 text   NOT NULL,
    applied        boolean NOT NULL,
    received_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, event_id, seq)
);

CREATE INDEX IF NOT EXISTS case_event_case_idx ON case_event (source, case_id, version DESC);

-- Rejected events. The primary key is the contract's "distinct rejected
-- source/event_id pairs", so quarantine_count cannot drift on replay.
-- detail carries field names and codes only, never values.
CREATE TABLE IF NOT EXISTS quarantine (
    source        text   NOT NULL,
    event_id      text   NOT NULL,
    seq           bigint,
    reason_codes  text[] NOT NULL,
    detail        jsonb  NOT NULL DEFAULT '{}'::jsonb,
    occurrences   integer NOT NULL DEFAULT 1,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, event_id)
);

-- Evidence that access control ran, for both allowed and denied calls.
-- No tokens, no contacts: sub is the identity fixture's subject claim.
CREATE TABLE IF NOT EXISTS access_audit (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    sub         text,
    role        text,
    route       text NOT NULL,
    decision    text NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason      text,
    http_status integer NOT NULL,
    unit_filter text
);

CREATE INDEX IF NOT EXISTS access_audit_at_idx ON access_audit (at DESC);
