# Decision log

Each entry states the decision, the alternative that was actually considered,
and why it lost **for this server and this data size**. Several would flip at a
different scale, and those thresholds are named.

---

## D1 — PostgreSQL as the store

**Decision.** PostgreSQL 16 in its own container, private network, no published
port, named volume.

**Alternative: SQLite.** Genuinely viable here. 9,000 cases is small, it would
remove a container and roughly 60 MiB, and WAL mode handles concurrent readers.

**Why not.** The correctness model of this pipeline is one statement:
`INSERT … ON CONFLICT (source, case_id) DO UPDATE … WHERE excluded.version > case_current.version`,
committed together with the cursor. Postgres gives that directly, along with
`text[]`, `jsonb`, partial indexes and `AT TIME ZONE` for the business-day
conversion. With SQLite the single writer lock would be held by the pollers
during report reads, and the version-wins upsert becomes hand-rolled.

**Also rejected: DuckDB.** Analytics-shaped and excellent at the report query,
but a poor fit for continuous concurrent writes while serving reads.

**When this flips.** If the deployment budget were a few hundred MiB total, or
the service had to run without a second container, SQLite would be the right
answer and the upsert would be written by hand with a test around it.

---

## D2 — One process for the API and all three pollers

**Decision.** A single container runs FastAPI and three asyncio poller tasks,
with `ROLE=api|worker|all` (default `all`) to split them later.

**Alternative: a separate worker container.** Textbook separation: an ingestion
crash cannot take down the read path, and each scales independently.

**Why not.** The pollers are I/O bound and idle most of the time — measured 46%
of one CPU during a load test that also had eight clients hammering reports.
A second container costs a second connection pool, a second image, a second unit
to restart and a second thing to reason about, to isolate a workload that is not
competing. On one small server that is a worse trade.

**The risk, named.** A pathological report query would compete with the pollers
for the same event loop. At 8,850 rows it does not: p95 is 203 ms and the
pollers kept converging throughout the load test. The `ROLE` flag is the escape
hatch, and it is configuration rather than a rewrite.

---

## D3 — No orchestrator

**Decision.** Plain asyncio loops. No Airflow, Dagster or Prefect.

**Alternative: Airflow**, which I have used on a comparable project.

**Why not.** An orchestrator earns its keep with dependency graphs, backfills
across partitions, retries with visible task state and a scheduling calendar.
Here there are three identical loops that never fan out, never depend on each
other, and must run continuously rather than on a schedule. Airflow would add a
scheduler, a webserver, its own metadata database and several hundred MiB, and
its unit of retry — the task — is the wrong granularity for "retry this cursor
in 1.2 seconds because the gateway said `Retry-After: 1`".

**When this flips.** As soon as there is a second stage that depends on the
first completing — a nightly reconciliation, a downstream export with its own
SLA — the scheduling problem becomes real and an orchestrator stops being
overhead.

---

## D4 — Cursor advances inside the applying transaction

**Decision.** One transaction per page covering `event_seen`, `case_event`,
`case_current`, `quarantine` and `source_state.cursor`.

**Alternative: apply the page, then update the cursor.** Simpler to write, and
the window is small.

**Why not.** The window is exactly where the bug lives. A crash between the two
loses a page permanently, because the cursor said it was consumed. Nothing later
detects it: the totals are simply wrong and stay wrong. Making it one
transaction turns at-least-once delivery into exactly-once business state, and
costs nothing at this page size.

**Evidence.** Replay from cursor 0 leaves `case_current`, the quarantine count
(100) and `event_seen` (9,600) identical.

---

## D5 — Version-wins upsert instead of an event-sourced rebuild

**Decision.** Maintain current state directly, with an append-only event ledger
alongside for lineage.

**Alternative: store events only, and derive the view.** Perfect auditability,
and any historical question becomes answerable.

**Why not.** The brief asks for "one reliable operational view" and daily
numbers that people trust, not time travel. Deriving state per query costs more
than storing it, and a rebuild-on-read design would have to solve exactly the
same version-wins problem anyway — just repeatedly instead of once. The event
ledger keeps the audit trail without paying for the rebuild.

**What we gave up.** We cannot reconstruct what the report said last Tuesday.
`case_event` stores payload hashes, so we can prove which delivery produced the
current state, but not reproduce a superseded one.

---

## D6 — No materialised aggregate

**Decision.** `/reports/daily` is a `GROUP BY` over `case_current` behind a
partial index.

**Alternative: a materialised view or a rollup table** refreshed on write.

**Why not.** Measured p95 is 203 ms against a 500 ms target with 8,850 active
rows and eight concurrent clients. There is no latency left to buy. A rollup
would add refresh scheduling, a staleness window, and invalidation logic that
must itself get the version-wins and tombstone rules right — a second place for
the same bug.

**When this flips.** Roughly two orders of magnitude more data, or a report that
spans history rather than current state. The measurement, not the preference, is
what should trigger the change.

---

## D7 — Quarantine instead of fail-fast

**Decision.** A structurally invalid event is recorded in `quarantine` with
reason codes; the cursor advances past it.

**Alternative: stop the source on a validation failure**, the strict-contract
approach used in my previous project's quality gate, where bad data blocks
publication.

**Why not.** That gate protects a batch publish, where stopping is cheap and
correct. Here the requirement is the opposite: "invalid events must remain
diagnosable without stopping all ingestion". 100 of 10,000 events are
deliberately malformed. Halting would mean one vendor defect freezes a whole
region's numbers.

**What keeps it honest.** The quarantine primary key is `(source, event_id)`, so
`quarantine_count` is the contract's "distinct rejected pairs" and cannot be
inflated by replay. `detail` stores field **names** and codes, never values —
a rejected value is precisely the one we are least sure is safe to store.

---

## D8 — Business identity is `(source, case_id)`

**Decision.** Composite key. No cross-vendor merging.

**Alternative: treat `case_id` as the business key**, merging the three sources
into one case per ID.

**Why not.** It would be catastrophically wrong. Each source independently emits
`C00001`…`C03000`, so merging would collapse 9,000 real cases into 3,000 and
triple-count money. The contract says so explicitly and the fixture proves it.

**What a real merge would need.** A master mapping, a survivorship rule per
field, and a confidence threshold — the "agency mapping candidate" idea from my
previous project, where candidate matches are stored separately from approved
ones. That is a project, not a field.

---

## D9 — Decimal money, exactness enforced

**Decision.** Birch major-unit strings convert via
`Decimal(raw) * 100`, and a non-integral result is quarantined as
`inexact_minor_units`.

**Alternative: `round()` to the nearest minor unit.**

**Why not.** Rounding invents money. If a vendor sends three decimal places, the
right response is to notice, not to absorb it.

**A bug this caught.** The first implementation used
`Decimal.to_integral_exact()`, assuming it raises on inexactness. It does not —
it only *signals* `Inexact`, and the default decimal context has that trap
disabled, so `143.6449` silently became `14364`. Money loss with no error and no
log line. The unit test found it before the network did; the code now compares
against `to_integral_value()` explicitly.

---

## D10 — Business timezone is configuration, not a constant

**Decision.** `BUSINESS_TIMEZONE`, defaulting to `Asia/Bangkok`.

**Alternative: cast to date in UTC**, which is what `::date` does by default.

**Why not.** Measured, the same data produces 540 report rows in UTC and 558 in
Asia/Bangkok, with different per-day totals and a date range that extends a day
further. That is precisely the "two people export the same report and get
different answers" complaint in the brief. A default that is invisible is how
that happens; a named setting with a documented rationale is how it stops.

See [CLARIFYING_QUESTIONS.md](CLARIFYING_QUESTIONS.md) question 1.

---

## D11 — Clearance filters aggregates, behind a flag

**Decision.** `CLEARANCE_FILTERS_AGGREGATES=true`. An internal-clearance caller
sees only `classification='internal'` cases, in reports as well as case detail.

**Alternative: clearance gates case detail only**, so aggregates cover every
case in a permitted unit.

**Why this default.** It makes `clearance` mean one consistent thing on both
routes, and under-disclosure is the safer failure when the answer is unknown.

**The cost, measured.** For `analyst_aster` on AST-1 that is 1,180 cases and
90,216,320 rather than 1,475 and 112,767,732 — so two analysts can legitimately
see different totals for the same unit. That tension is real, it is question 2
on the clarification list, and it is one environment variable to flip.

---

## D12 — Policy as a table

**Decision.** `ROUTE_ROLES`, `ALLOWED_PURPOSES` and the unit/clearance
predicates are data, evaluated by one `guard` every business route calls.

**Alternative: per-route conditionals.**

**Why not.** The brief says the interviewer may introduce a bounded change
during review. A table makes "auditors should not see reports" a one-line edit I
can make live and re-test in seconds. Scattered conditionals make it a hunt, and
they are how a new route ships with no check at all.

---

## D13 — Fail closed on identity, with a 5-second cache

**Decision.** Introspection cached for at most 5 s, keyed by a token hash. Once
an entry expires and identity is unreachable, business routes return 503.

**Alternative: serve from a stale cache during an identity outage**, trading a
little correctness for availability.

**Why not.** The contract mandates failing closed, and it is right to: an expired
entry is exactly the case where a token may have just been revoked. Availability
of a report is worth less than not serving data to someone who lost access.
`/health` stays 200 throughout, so the orchestrator does not restart a service
whose only problem is a dependency.

**Verified.** With identity stopped, `/reports/daily` returns 503 and `/health`
returns 200; both recover within 3 s of identity returning.

---

## D14 — Client rate budget below the published ceiling

**Decision.** aster 6/s against a ceiling of 8, birch 3/s against 4, cobalt
1.5/s against 2, one in-flight request per source.

**Alternative: run at the ceiling and handle 429s.**

**Why not.** The ceiling is enforced source-wide across all connections, so
there is nothing to win by crowding it — and each 429 costs a full second of
`Retry-After`, which is far more than the throughput gained. Cold convergence is
22 seconds against a 120-second target, so the headroom is free.

---

## D15 — Handle the injected fault rather than avoiding it

**Decision.** `PAGE_LIMIT=200`, which makes cursors land on 600, 1200, 1800,
2400 and 3000 — every value the fixture fails on first request.

**Alternative: a page size that never lands on a multiple of 600.**

**Why not.** It would pass the exercise while proving nothing. The injected
fault is there to test retry, so the right answer is to hit it 15 times across
three sources and recover from all 15, which the logs show.
