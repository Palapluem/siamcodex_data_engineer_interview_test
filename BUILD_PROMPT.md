# BUILD PROMPT — Meridian operational view

A single, self-contained instruction set for building the Siam Codex data-engineer
take-home. Hand this file to an implementing agent (or work through it yourself)
together with [docs/CONTRACT_FACTS.md](docs/CONTRACT_FACTS.md), which holds the
verified ground truth this prompt refers to.

---

## 0. Role and mission

You are a senior data engineer. Build a containerised, production-minded pipeline
that ingests three vendor case systems through authenticated TLS gateways,
resolves them into one trustworthy operational view, and serves that view over an
authorised HTTP API on a single small server.

The graded product is **a complete, explainable core path plus honest evidence** —
not a large platform. Every component you add must be one you can defend in a
60-minute review and modify live.

**Read before writing code**, in this order:

1. `candidate/REQUIREMENTS.md` — the business brief
2. `candidate/TECHNICAL_CONTRACT.md` — the fixed, testable interfaces
3. `candidate/README.md` — lab operation and submission rules
4. `docs/CONTRACT_FACTS.md` — verified API behaviour, field mapping, expected numbers

---

## 1. Non-negotiable guardrails

Violating any of these fails the assessment regardless of code quality.

**Data access**
- The service obtains business data **only** through `https://vpn-<source>:8443/v1/changes`.
  Never read, mount, copy or bake in `candidate/data/*.jsonl` — not even for seeding,
  warm-up or tests that run inside the service container.
- Verify TLS against `ca.crt`, checking **issuer and hostname**. Never set
  `verify=False`, never add a permissive SSL context, never pin to an IP.

**Network**
- Join only `de-interview-transit` (external) and your own private app network.
- Publish **only** `127.0.0.1:8088 -> 8080`. The database gets no published port.
- No host networking, `extra_hosts`, `NET_ADMIN`, privileged mode, added routes,
  or Docker socket access. No second compose file overriding supplied services.

**Secrets**
- Mount read-only, individually: `.runtime/client/ca.crt`,
  `.runtime/client/source_credentials.json`, `.runtime/client/identity_service.key`.
- **Never** mount `.runtime/client/test_tokens.json` into the service. Never touch
  `.runtime/server` or the CA private key.
- No credential ever enters an image layer, a log line, an error body, a test
  fixture or a commit.

**Identity**
- Roles, units, clearance and purpose come from `POST /introspect` only.
  Reject and ignore `X-Role`, `X-Units`, `X-Clearance` and every similar header.
- Cache introspection for **at most 5 seconds**. When the cache entry has expired
  and identity cannot be reached, **fail closed** on business routes.

**Fixed inputs**
- `candidate/infrastructure/`, `candidate/data/`, `candidate/scripts/`,
  `candidate/README.md`, `candidate/REQUIREMENTS.*` and
  `candidate/TECHNICAL_CONTRACT.md` are read-only exercise inputs. All of your work
  lives under `candidate/submission/` plus repo-level `docs/`.
- Never run a broad `docker prune`. Never delete lab volumes during restart testing.

---

## 2. Architecture to build

Two services in the compose project `de-interview-submission`.

```
                     127.0.0.1:8088
                           |
              +------------v-------------+
              |  pipeline  (FastAPI)     |         de-interview-transit
              |  ---------------------   |        (external, internal-only)
              |  API   : /health /status |            |
              |          /reports/daily  |   TLS +    +-- vpn-aster:8443   -> aster_private
              |          /cases          |  bearer    +-- vpn-birch:8443   -> birch_private
              |  Worker: 3 async pollers |<-----------+-- vpn-cobalt:8443  -> cobalt_private
              |          (one per source)|            +-- identity:8443
              +------------+-------------+
                           | app network (private, no published ports)
                     +-----v------+
                     | postgres16 |  named volume: pipeline_pgdata
                     +------------+
```

**Why one process for API and workers:** on one small server, three async pollers
are I/O-bound and cost almost nothing next to a separate container, a second
connection pool and a second deployment unit. Keep them separable with an env
flag `ROLE=all|api|worker` (default `all`) so splitting later is configuration,
not a rewrite. Say exactly this in the decision log, and name the tradeoff: a
pathological report query competes with pollers for the same event loop — at 9,000
rows it does not, and the flag is the escape hatch if it ever does.

**Why PostgreSQL:** the whole correctness story is "highest version wins, applied
idempotently, cursor advanced in the same transaction". That is one
`INSERT ... ON CONFLICT DO UPDATE ... WHERE excluded.version > case_current.version`
inside a transaction. Postgres gives it directly, plus concurrent readers during
ingest. Compare against SQLite in the decision log (smaller and genuinely viable at
this scale; rejected for single-writer locking under concurrent report load and a
weaker upsert story) and against DuckDB (analytics-shaped, poor concurrent write).

**Why no materialised aggregate:** 8,850 active rows. A plain `GROUP BY` over an
indexed table answers in single-digit milliseconds, far inside the 500 ms p95
target. Materialising would add refresh, staleness and invalidation bugs for no
measured gain. Document the threshold at which you would revisit it, and show the
measurement that justifies the choice.

---

## 3. Repository layout

```
siamcodex_data_engineer_interview_test/
├── README.md                       # front page: what, how to run, where to look
├── .gitattributes .gitignore
├── candidate/                      # fixed exercise inputs, hash-verified, unchanged
│   ├── data/ infrastructure/ scripts/
│   ├── REQUIREMENTS.md  REQUIREMENTS.pdf  TECHNICAL_CONTRACT.md  README.md
│   └── submission/                 # ALL OF OUR CODE
│       ├── compose.yaml
│       ├── Dockerfile
│       ├── requirements.txt        # fully pinned, hash-free but exact versions
│       ├── app/
│       │   ├── __init__.py  main.py  config.py  db.py
│       │   ├── migrations/001_init.sql
│       │   ├── ingest/
│       │   │   ├── client.py       # TLS session, token bucket, retry/backoff
│       │   │   ├── normalize.py    # per-vendor adapters + validation
│       │   │   ├── apply.py        # transactional upsert + cursor advance
│       │   │   └── poller.py       # per-source loop, state machine
│       │   ├── api/
│       │   │   ├── auth.py         # introspection, cache, policy engine
│       │   │   ├── deps.py  health.py  status.py  reports.py  cases.py
│       │   └── obs/
│       │       ├── logging.py      # structured JSON + redaction
│       │       ├── audit.py        # append-only access audit
│       │       └── metrics.py      # counters/histograms surfaced via /status
│       ├── tests/
│       │   ├── unit/               # no network, no docker
│       │   └── integration/        # against the running lab
│       └── tools/
│           ├── lab_control.py      # flip phase / outage / revoke (test harness only)
│           ├── loadtest.py         # 8 clients x 30s, p50/p95/p99
│           ├── resource_sample.py  # docker stats sampler -> CSV
│           └── verify_fixtures.py  # sha256 vs manifest
└── docs/
    ├── CONTRACT_FACTS.md           # verified ground truth (already written)
    ├── PLAN.md                     # execution plan
    ├── ARCHITECTURE.md             # + diagram, deployment units, data flow
    ├── TRUST_BOUNDARIES.md         # + data classification, secrets, retention
    ├── DECISIONS.md                # decision log with rejected alternatives
    ├── ASSUMPTIONS.md              # assumption register with impact-if-wrong
    ├── CLARIFYING_QUESTIONS.md     # questions for the interviewer
    ├── RUNBOOK.md                  # setup, operate, recover, rotate, add a source
    ├── RESOURCE_REPORT.md          # measured, with commands and hardware
    ├── KNOWN_LIMITATIONS.md
    ├── TIME_LOG.md  AI_DISCLOSURE.md
    └── SUBMISSION_FORM_ANSWERS.md
```

---

## 4. Data model

Schema `meridian`, one migration file applied idempotently at startup.

```sql
-- Per-source ingestion state. One row per source, created at startup.
CREATE TABLE source_state (
  source              text PRIMARY KEY,
  cursor              text        NOT NULL DEFAULT '0',
  state               text        NOT NULL DEFAULT 'starting',   -- starting|healthy|degraded
  last_success_at     timestamptz,
  last_attempt_at     timestamptz,
  last_error_kind     text,          -- rate_limited|transient|outage|auth|schema|network
  consecutive_failures int          NOT NULL DEFAULT 0,
  events_ingested     bigint       NOT NULL DEFAULT 0
);

-- Idempotency ledger. PK makes replay a no-op and makes quarantine counting exact.
CREATE TABLE event_seen (
  source     text NOT NULL,
  event_id   text NOT NULL,
  first_seq  bigint NOT NULL,
  last_seq   bigint NOT NULL,
  deliveries int NOT NULL DEFAULT 1,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, event_id)
);

-- Current business truth. Business identity is (source, case_id).
CREATE TABLE case_current (
  source         text   NOT NULL,
  case_id        text   NOT NULL,
  unit_id        text,
  version        int    NOT NULL,
  event_time     timestamptz,
  amount_minor   bigint,
  currency       text,
  status         text,               -- open|completed|cancelled
  classification text,               -- internal|restricted
  contact        text,               -- auditor-only egress
  is_deleted     boolean NOT NULL DEFAULT false,
  last_event_id  text,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, case_id)
);
CREATE INDEX case_current_report_idx
  ON case_current (unit_id, status) WHERE NOT is_deleted;

-- Append-only lineage so the assurance team can trace a case to its event.
-- Hash only; never store the raw payload.
CREATE TABLE case_event (
  source        text   NOT NULL,
  event_id      text   NOT NULL,
  seq           bigint NOT NULL,
  case_id       text,
  version       int,
  op            text,
  schema_version int,
  payload_sha256 text  NOT NULL,
  applied        boolean NOT NULL,   -- false = superseded by a higher version
  received_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, event_id, seq)
);

-- Rejected events. PK gives "distinct rejected source/event_id pairs" for free.
CREATE TABLE quarantine (
  source       text NOT NULL,
  event_id     text NOT NULL,
  seq          bigint,
  reason_codes text[] NOT NULL,
  detail       jsonb,               -- field names and codes only, never values
  occurrences  int NOT NULL DEFAULT 1,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, event_id)
);

-- Evidence that access control ran. No tokens, no contacts.
CREATE TABLE access_audit (
  id          bigserial PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  sub         text,
  role        text,
  route       text NOT NULL,
  decision    text NOT NULL,        -- allow|deny
  reason      text,                 -- role|purpose|unit|clearance|unauthenticated
  http_status int  NOT NULL,
  unit_filter text
);
```

Reporting is a plain query, not a table:

```sql
SELECT (event_time AT TIME ZONE :business_tz)::date AS date,
       unit_id, status, count(*) AS case_count, sum(amount_minor) AS amount_minor
FROM case_current
WHERE NOT is_deleted
  AND unit_id = ANY(:allowed_units)
  AND (:include_restricted OR classification = 'internal')
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

---

## 5. Ingestion specification

One `asyncio` task per source, fully independent: own client, own token bucket,
own backoff, own state row. **A failure in one source must never pause another** —
no shared semaphore, no shared retry lock, no `gather` that aborts siblings.

**Loop**

1. Read `cursor` from `source_state` at startup (never from memory alone).
2. `GET /v1/changes?cursor=<cursor>&limit=200`.
3. Normalise and validate every item.
4. In **one transaction**: upsert `event_seen`, insert `case_event`, apply valid
   events to `case_current`, insert or bump `quarantine` rows, then write
   `next_cursor` and `last_success_at` into `source_state`. Commit.
5. If `has_more` → continue immediately (bucket permitting). Otherwise sleep the
   idle interval (**3 s**) and poll again — new events appear even after
   `has_more=false`.

**Rate control.** Client-side token bucket strictly below the ceiling:
aster 6/s, birch 3/s, cobalt 1.5/s. One in-flight request per source.

**Failure handling**

| Response | Action | State |
|---|---|---|
| `429` | Honour `Retry-After`, then retry the same cursor | keep current state |
| `503` transient / `link_unavailable` | Honour `Retry-After`, exponential backoff with jitter, cap 30 s, retry same cursor | `degraded` after 3 consecutive failures |
| `401` | Do **not** retry-storm. Back off to 30 s, reload credentials from the mounted file, log `auth` error kind with no token material | `degraded` |
| `400` | Bug in our request construction — log loudly, back off, do not advance cursor | `degraded` |
| timeout / connection error | Backoff with jitter, retry same cursor | `degraded` after 3 |
| `200` | Reset `consecutive_failures`, set `last_success_at`, advance cursor | `healthy` |

Timeouts: connect 3 s, read 10 s (the gateway upstream timeout is 5 s).

**Validation → quarantine reason codes.** Never let a bad event stop the stream;
quarantine it, advance past it, keep it diagnosable:

`missing_identifier`, `missing_version`, `invalid_event_time`, `invalid_amount`,
`inexact_minor_units`, `unknown_status`, `unknown_unit`, `unsupported_schema_version`,
`unknown_op`, `malformed_payload`.

`detail` records field **names** and codes only. A contact or amount value must
never reach the quarantine table, a log line or an error body.

**Apply semantics**

- Business identity is `(source, case_id)`. Case IDs collide across vendors.
- Highest `version` wins. Equal or lower version is ignored — this is what makes
  duplicate delivery and full replay safe.
- `op=delete` is a tombstone: set `is_deleted=true`, bump `version`, **retain the
  previously known attributes** so lineage survives. Reports exclude it; `/cases`
  returns 404 for it.
- A tombstone arriving before any upsert creates an identity-only deleted row. A
  later lower-version upsert stays ignored. Note this in known limitations rather
  than inventing resurrection rules.
- Money: `Decimal` only. Birch major-unit strings become minor units via
  `int((Decimal(raw) * 100).to_integral_exact())`; a non-exact conversion is
  `inexact_minor_units`, not a rounding.
- Birch `schema_version=2` reads `net_amount`; v1 reads `amount`. An unknown
  `schema_version` is quarantined, not guessed.

---

## 6. API specification

Listen on container port `8080`. All responses JSON. Row objects contain
**exactly** the specified fields — no extras, because extra fields leak.

### `GET /health`
Unauthenticated. `200` when the process is alive. Does **not** report source
freshness and does not touch the database on the hot path.

### `GET /status` — operator only
```json
{"sources":{"aster":{"cursor":"3334","state":"healthy","last_success_at":"2026-09-06T12:00:00Z"},
            "birch":{...},"cobalt":{...}},
 "quarantine_count": 100}
```
`state` ∈ `starting|healthy|degraded`. `quarantine_count` is distinct rejected
`(source, event_id)` pairs. Every other role gets `403` — operational visibility
is not business access. No payloads, no credentials.

### `GET /reports/daily[?unit_id=AST-1]`
```json
{"rows":[{"date":"2026-06-01","unit_id":"AST-1","status":"completed","case_count":12,"amount_minor":12345}]}
```
- Sorted by `date`, `unit_id`, `status`. One row per non-empty group; **no
  zero-filled groups**.
- Counts current, non-deleted cases, each at its latest version, exactly once.
- Unknown `unit_id` (outside the fixed allowlist) → **400**.
- Known but unauthorised `unit_id` → **403**, *including when it holds no data*
  (checking authorisation before data existence prevents a probing oracle).
- No `unit_id` → the caller's authorised subset, aggregated.
- Never contains `contact`, case identifiers or raw payloads.

### `GET /cases?source=aster&case_id=C00001` — auditor only
```json
{"item":{"source":"...","case_id":"...","unit_id":"...","version":1,
         "event_time":"2026-06-30T15:43:00Z","amount_minor":148598,"currency":"THB",
         "status":"completed","classification":"restricted","contact":"..."}}
```
Exactly those ten fields. Missing or unknown `source` → `400`. Non-auditor →
`403`. Auditor, unknown or deleted case → `404`.

### Status-code discipline
`401` for missing, malformed, unknown or revoked credentials. `403` for a valid
caller forbidden by role, purpose, unit or clearance. Order matters: authenticate,
then authorise, then validate input against allowlists, then query. An unknown
input must never widen a query.

### Authorisation matrix — implement exactly, keep it table-driven

| Caller | `/health` | `/status` | `/reports/daily` | `/cases` |
|---|---|---|---|---|
| no / bad / revoked token | 200 | **401** | **401** | **401** |
| `analyst_aster` | 200 | **403** | 200, AST-1 only; other units 403 | **403** |
| `analyst_all` | 200 | **403** | 200, all units | **403** |
| `auditor` | 200 | **403** | 200, all units | **200** |
| `operator` | 200 | **200** | **403** | **403** |
| `wrong_purpose` | 200 | **403** | **403** | **403** |

Encode this as data (a policy table), not scattered `if` statements, so a
clarification-sheet change is a one-line edit you can make live in the review.

Allowed purposes for business data: `operations`, `audit`. `marketing` is denied —
that is what the `wrong_purpose` fixture exists to prove.

Clearance: `internal` sees `classification='internal'` only; `restricted` sees
everything. Controlled by `RESTRICTED_IN_AGGREGATES`; see assumption 2.

---

## 7. Observability, audit and secrets

- **Logs**: structured JSON, one line per event, with `source`, `event`, `outcome`,
  `duration_ms`, `cursor`, `sub` — never `token`, never `contact`, never a raw
  payload. Add a redaction filter that scrubs any value matching a token or email
  pattern, so an accidental interpolation still cannot leak. Prove it with a test.
- **Audit**: every business-data request writes one `access_audit` row —
  allow or deny, with the reason. This is the "audit evidence" the brief asks for.
- **Metrics**: counters (`events_ingested`, `events_quarantined`, `poll_attempts`,
  `poll_failures` by kind, `authz_denials` by reason) and a request-latency
  histogram, surfaced inside operator-only `/status` rather than a new open port.
- **Retention**: state the policy even though the exercise is short-lived —
  `case_current` indefinite (operational truth), `case_event` 90 days,
  `quarantine` 30 days after resolution, `access_audit` 1 year, logs 14 days.
  Contacts live in exactly one column, egress only through the auditor route.
- **Secrets**: read the mounted files at startup and on `401`. Never log them,
  never put them in an image, never echo them in an error. Rotation is a file
  swap plus a restart or a re-read — document the exact commands.

---

## 8. Resource allocation

Set explicit limits and justify them with measurements, not vibes.

| Service | cpus | mem_limit | Why |
|---|---|---|---|
| `pipeline` | 1.0 | 512m | Async I/O bound; three pollers plus FastAPI at 9k rows |
| `postgres` | 0.75 | 512m | `shared_buffers=128MB`, `max_connections=30`; dataset well under 100 MB |

`pipeline` runs `read_only: true`, `cap_drop: [ALL]`,
`security_opt: [no-new-privileges:true]`, `pids_limit: 128`, `tmpfs: /tmp`, non-root
user. Postgres needs a writable data directory, so it gets the named volume
`pipeline_pgdata` and no published port; document why `read_only` is not applied
there. Total persistent state must stay under **2 GiB** — measure and report it.

---

## 9. Acceptance criteria

Numbers below are for seed 73129 and are the evidence for our documented run.
Tests assert the **invariants**; the report cites the numbers.

**Correctness — phase 1 baseline**
- `/status` → all three sources `healthy`, `quarantine_count == 100`
- 9,000 distinct `(source, case_id)`, 0 deleted
- `/reports/daily` as `analyst_all` → **557 rows** (Asia/Bangkok), case_count sum
  9,000, amount_minor sum **681,046,850**

**Correctness — after phase 2 release**
- 8,850 active, 150 tombstoned, `quarantine_count` still **100**
- `/reports/daily` → **558 rows**, case_count sum 8,850, amount_minor sum **670,152,982**
- `C00101`–`C00250` show version 2 with the corrected amount and `completed`
- `C00251`–`C00300` return **404** on `/cases` and appear in no report row

**Invariants that must hold on any seed**
- Rerunning ingestion from cursor 0 changes no aggregate and no quarantine count
- Every `case_id` appears exactly once per source in the report source set
- `sum(case_count)` equals the count of active cases
- No report response contains `contact`, `case_id` or a raw payload

**Timing** — baseline converge < 120 s; new events < 60 s; 30 s outage on one
source leaves the others polling and catches up < 60 s after restore;
`/reports/daily` p95 ≤ 500 ms at 8 concurrent clients over 30 s.

**Resilience**
- `docker compose restart pipeline` with volumes retained → identical aggregates,
  ingestion resumes from the stored cursor
- Postgres restart → pipeline reconnects without manual intervention
- Revoked token → `401` within 5 s (cache TTL), not later
- Identity unreachable → business routes fail closed with `503`, `/health` stays `200`

**Security**
- The full authorisation matrix passes, allow **and** deny
- `X-Role: auditor` on an analyst token changes nothing
- `grep -ri` over logs and responses finds no token and no `@.*example.invalid`
- `docker history` on the image shows no credential

---

## 10. Build order

Each phase ends with a runnable checkpoint. Do not start the next phase until the
current checkpoint is green. Full timing is in [docs/PLAN.md](docs/PLAN.md).

| # | Phase | Checkpoint |
|---|---|---|
| 0 | Lab up | `bootstrap.py` run, `compose up --wait` green, `probe` passes all checks |
| 1 | Skeleton | `docker compose -f submission/compose.yaml up` → `/health` 200, migration applied |
| 2 | Ingestion | All three sources converge; counts match phase-1 acceptance |
| 3 | API + authz | Full matrix passes with the five fixture tokens |
| 4 | Tests | `pytest` green; unit tests offline, integration tests against the lab |
| 5 | Resilience | Outage, phase-2 release, revocation and restart scenarios all pass |
| 6 | Performance | Load test and resource sampling captured to `docs/RESOURCE_REPORT.md` |
| 7 | Docs | Architecture, trust boundaries, decisions, runbook, limitations, time log |
| 8 | Submit | Clean-clone verification, push, form submitted |

**Cut in this order if time runs short:** `/metrics` detail → `case_event` lineage
table → the `ROLE` split flag → load test shortened to 15 s → merge the ADRs into
one `DECISIONS.md`. **Never cut:** version-wins correctness, the authorisation
matrix, restart safety, or the assumptions and questions documents.

---

## 11. Anti-patterns that will cost marks

- Choosing `limit` to dodge the injected `cursor % 600` failure instead of retrying.
- Advancing the cursor outside the transaction that applies the page — this is the
  classic silent data-loss bug and the reviewer is looking for it.
- Deduplicating on `event_id` alone, or keying business identity on `case_id` alone.
- `float` anywhere near money.
- `WHERE unit_id = :unit` with no allowlist check, so an unknown unit silently
  returns everything or nothing.
- Checking data existence before authorisation, which turns 403 into a 404 oracle.
- Trusting the introspection cache past 5 s, or failing **open** when identity is down.
- Returning extra fields inside row objects because they were convenient.
- A `/status` that leaks payloads or credentials.
- Claiming performance numbers you did not measure, or presenting estimates as
  measurements. Label every number.
- Silent generalisation: a plugin framework for a fourth source nobody asked for.
  Say where you deliberately stopped generalising and why.

---

## 12. Definition of done

- `docker compose -f candidate/submission/compose.yaml up -d --build` on a clean
  clone, with freshly bootstrapped credentials, reaches the phase-1 acceptance
  numbers unattended.
- `pytest` passes; integration tests are marked and skip cleanly without the lab.
- Every document in `docs/` is written, and every number in `RESOURCE_REPORT.md`
  is labelled **measured** or **estimated** with the command that produced it.
- `docs/ASSUMPTIONS.md` lists each assumption with what changes if it is wrong.
- `docs/CLARIFYING_QUESTIONS.md` is prioritised, not exhaustive.
- `docs/AI_DISCLOSURE.md` is honest and specific.
- `.runtime` is not committed; `git log -p | grep` finds no secret.
- You can explain and modify every line under review.
