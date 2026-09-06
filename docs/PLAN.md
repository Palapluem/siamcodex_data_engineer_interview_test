# Execution plan — 6 September 2026

Window: 12:00–23:59 (12 h). Planning consumed the first ~30 min. This plan runs
**12:30 → 22:00 build**, **22:00 freeze**, **22:30 submit**, leaving ~1.5 h buffer.
The brief expects 10–14 focused hours, so the honest position is: build the core
path completely, measure it, document it, and be explicit about what stayed a
proposal. That is what the rubric rewards.

Companion documents: [BUILD_PROMPT.md](../BUILD_PROMPT.md) (what to build),
[CONTRACT_FACTS.md](CONTRACT_FACTS.md) (verified ground truth).

> **This is the plan as written at 12:30, before implementation.** The build
> finished far ahead of it — every phase checkpoint was met, but at 13:20
> rather than 22:00. [TIME_LOG.md](TIME_LOG.md) has the actual timings and an
> honest account of why. The plan is kept unedited because the schedule it
> set, the risk register and the cut-scope ladder are what the work was
> steered by, and a plan rewritten after the fact proves nothing.

---

## Schedule

| Time | Phase | Output | Checkpoint |
|---|---|---|---|
| 12:30–13:15 | **0 — Lab up** | Docker Desktop started, `bootstrap.py` run, lab healthy, probe green, first real page inspected | `probe` prints all four OK lines |
| 13:15–14:15 | **1 — Skeleton** | compose, Dockerfile, pinned deps, config, DB migration, `/health` | `up -d --build` → `curl 127.0.0.1:8088/health` = 200 |
| 14:15–16:15 | **2 — Ingestion** | client, token bucket, retry, 3 normalisers, transactional apply, quarantine, cursor | Phase-1 numbers match: 9,000 cases, 100 quarantined |
| 16:15–17:45 | **3 — API + authz** | introspection + 5 s cache, policy table, `/status`, `/reports/daily`, `/cases` | Full matrix passes by hand with the 5 fixture tokens |
| 17:45–18:45 | **4 — Tests** | unit (offline) + integration (lab), allow and deny | `pytest` green |
| 18:45–19:45 | **5 — Resilience** | outage, phase-2 release, revocation, restart, replay | All five scenarios pass, evidence captured |
| 19:45–20:45 | **6 — Performance** | load test, resource sampling, storage size | `RESOURCE_REPORT.md` with measured numbers |
| 20:45–22:00 | **7 — Docs** | architecture + diagram, trust boundaries, decisions, runbook, assumptions, questions, limitations, time log, AI disclosure | Every doc written |
| 22:00–22:30 | **8 — Submit** | clean-clone verification, push, Google Form | Repo public, form submitted |
| 22:30–23:59 | Buffer | — | — |

---

## Phase 0 — Lab up (12:30–13:15)

Docker Desktop is currently **not running** on this machine — start it first and
confirm `docker info` reports the Linux engine before anything else.

```sh
cd candidate
python scripts/bootstrap.py
docker compose -f infrastructure/compose.yaml up -d --build --wait
docker compose -f infrastructure/compose.yaml --profile tools run --rm probe
```

`bootstrap.py` refuses to overwrite an existing `.runtime`. If it already exists,
keep it — do not delete it mid-exercise.

Then inspect one real page before writing an adapter, because the contract says the
illustrative payload may not match reality:

```sh
docker compose -f infrastructure/compose.yaml --profile tools run --rm \
  -v "$PWD/../scratch:/scratch" probe python -c "..."
```

Simplest alternative: a throwaway container on `de-interview-transit` that curls
one page per source with `limit=2` and pretty-prints it. Confirm against
[CONTRACT_FACTS.md](CONTRACT_FACTS.md) section 2 that the field names, the birch
`+07:00` offset and the cobalt `timestamp_ms` are exactly as expected. **Do not
skip this** — it is the cheapest possible check on the whole ingestion design.

Also verify fixture hashes still match the manifest (already done once; re-run
after any clone):

```sh
python candidate/submission/tools/verify_fixtures.py
```

**Risk:** Docker Desktop or the WSL backend fails to start. This is the one
blocker that can end the attempt. If it is not healthy by 13:15, mail the
interviewer immediately — setup failures outside our code do not count against
the assessment, and the earlier we ask, the better it reads.

---

## Phase 1 — Skeleton (13:15–14:15)

Build the thinnest thing that runs end to end, so every later phase has a place to
land.

- `candidate/submission/compose.yaml` from `compose.example.yaml`: project name
  `de-interview-submission`, `transit` external, private `app` network, publish
  only `127.0.0.1:8088`, the three read-only credential mounts, named volume.
- `Dockerfile`: `python:3.12-slim` pinned by digest, non-root user, no build
  toolchain in the final layer, `requirements.txt` fully pinned.
- `app/config.py`: env-driven settings — `BUSINESS_TIMEZONE`,
  `CLEARANCE_FILTERS_AGGREGATES`, `POLL_IDLE_SECONDS`, `PAGE_LIMIT`, per-source rate
  budget, `INTROSPECT_CACHE_SECONDS`, `ROLE`.
- `app/db.py`: async pool, startup migration runner that is safe to re-run.
- `/health` returning 200.

**Checkpoint:** `docker compose -f candidate/submission/compose.yaml up -d --build`
then `curl -s 127.0.0.1:8088/health`.

**Reuse from `thai-public-data-platform`:** the compose hardening shape
(`127.0.0.1`-bound publishes, healthchecks, named volumes), the migration-file
convention in `sql/postgres/00N_*.sql`, and the `pyproject.toml` ruff/pytest
configuration. Copy the patterns, not the Airflow machinery — an orchestrator is
the wrong deployment unit for three async pollers on one small server, and saying
so in the decision log is worth more than using one.

---

## Phase 2 — Ingestion (14:15–16:15) — critical path

This is the phase that must not slip. Build in this order and test each piece
offline before wiring it to the network.

1. **`normalize.py` first, with unit tests, no network.** Three adapters plus
   validation, driven by hand-written payload samples lifted from
   `CONTRACT_FACTS.md` section 2. Cover: aster v1, birch v1 decimal string, birch
   v2 `net_amount` with `amount` absent, cobalt `timestamp_ms`, all three status
   vocabularies, tombstones with only identity and version, and the four invalid
   shapes. This is the highest-density correctness work in the whole exercise and
   it needs no Docker, so it cannot be blocked by the lab.
2. **`client.py`** — TLS session against `ca.crt`, per-source token bucket,
   `Retry-After` handling, backoff with jitter, error classification.
3. **`apply.py`** — one transaction per page: `event_seen`, `case_event`,
   `case_current` upsert with `WHERE excluded.version > case_current.version`,
   `quarantine` upsert, then `source_state.cursor`. Commit once.
4. **`poller.py`** — the per-source loop and state machine.

**Checkpoint:** all three sources reach the phase-1 numbers. Verify directly
against the database before the API exists:

```sh
docker compose -f candidate/submission/compose.yaml exec -T postgres \
  psql -U pipeline -d meridian -c \
  "SELECT count(*) FILTER (WHERE NOT is_deleted) AS active,
          count(*) FILTER (WHERE is_deleted) AS deleted FROM meridian.case_current;
   SELECT count(*) FROM meridian.quarantine;"
```

Expect `9000 / 0` and `100`.

**Reuse:** the quarantine-instead-of-fail idea is the same shape as the existing
`quality/gate.py` + schema-contract split — validate against a declared contract,
record failures as evidence rather than exceptions, and keep the pipeline moving.
The cursor logic is the same reasoning as `public_sources/watermark.py`: a
watermark never moves backwards, and a replay must be safe.

---

## Phase 3 — API and authorisation (16:15–17:45)

Order matters: `auth.py` before any route, so no route can accidentally ship
without a policy decision.

- Introspection client with a 5 s TTL cache keyed by a **hash** of the token, never
  the token. Fail closed after expiry.
- Policy as a table: `(role, purpose, route) -> allow/deny`, plus unit scoping and
  clearance scoping applied as query predicates.
- `/status` (operator only), `/reports/daily`, `/cases` (auditor only).
- Every business request writes an `access_audit` row.

**Checkpoint:** run the matrix by hand with all five fixture tokens and record the
status codes. Read tokens on the **host**, never inside the service:

```sh
TOK=$(python -c "import json;print(json.load(open('candidate/.runtime/client/test_tokens.json'))['operator'])")
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOK" 127.0.0.1:8088/status
```

Expect exactly the matrix in `BUILD_PROMPT.md` section 6, including the awkward
cells: operator gets 403 on reports, `wrong_purpose` gets 403 everywhere,
`analyst_aster` gets 403 for `AST-2` **and** for `BIR-1`, and unknown `ZZZ-9`
gets 400.

---

## Phase 4 — Tests (17:45–18:45)

- **Unit, offline:** normalisers, money conversion, timezone bucketing, version-wins
  and tombstone logic, policy decisions, log redaction. These must run with no
  Docker so CI stays honest.
- **Integration, marked, against the lab:** convergence, the authorisation matrix,
  replay idempotency, restart safety.
- Assert **invariants**, not seed-specific totals, since the evaluation seed may
  differ. Keep the seed-73129 numbers in the resource report as evidence.

**Reuse:** the `pytest` layout and the `integration` marker convention from
`thai-public-data-platform/pyproject.toml`, and its CI workflow shape
(lint → contract validation → tests → image build).

---

## Phase 5 — Resilience scenarios (18:45–19:45)

Drive the lab with `tools/lab_control.py`, which rewrites
`.runtime/server/control/control.json` **atomically and re-padded to 4096 bytes**
(the fixture reads it on every request with a bounded retry). This is test
harness tooling on the host, not part of the service.

| Scenario | Command | Expected |
|---|---|---|
| Source outage | `outage birch on`, wait 30 s, `outage birch off` | birch → `degraded`, aster and cobalt keep polling and stay `healthy`, birch catches up < 60 s |
| New events | `phase 2` | 8,850 active / 150 deleted / 100 quarantined within 60 s |
| Token revocation | `revoke analyst_all` | 401 within 5 s |
| Restart | `docker compose restart pipeline` | identical aggregates, cursor resumes, no double counting |
| Full replay | reset cursors to 0 | aggregates and quarantine count unchanged |

Capture the before/after outputs into `evidence/` as they run — reconstructing
them at 21:30 wastes time you will not have.

---

## Phase 6 — Performance and resources (19:45–20:45)

- `tools/loadtest.py`: 8 concurrent clients, 30 s, `/reports/daily` with
  `analyst_all`; report p50/p95/p99, error count, throughput.
- `tools/resource_sample.py`: `docker stats --no-stream` on a 1 s interval into
  CSV; report peak and mean CPU and memory per service.
- Storage: `docker system df -v` for the named volume, plus
  `pg_database_size('meridian')`.
- Record: host CPU model, core count, RAM, Docker version, dataset size, and
  startup-to-convergence.

Label every number **measured** or **estimated**. Sampling does not prove the
absence of brief spikes — say so explicitly; the contract calls this out.

---

## Phase 7 — Documentation (20:45–22:00)

Write in this order, because the earlier documents are the ones that carry marks
if time runs out:

1. `ASSUMPTIONS.md` and `CLARIFYING_QUESTIONS.md` — the brief grades the quality of
   the questions, and these are already drafted, so they only need updating with
   what the build actually taught us.
2. `ARCHITECTURE.md` with a diagram — components, networks, trust boundaries, data
   flow, deployment units, and the fourth-source migration path.
3. `DECISIONS.md` — each decision, the rejected alternative, and why. At minimum:
   Postgres vs SQLite, single process vs split worker, no materialised aggregate,
   no orchestrator, quarantine vs fail-fast, cursor-in-transaction.
4. `TRUST_BOUNDARIES.md` — boundaries, data classification, secret handling,
   retention, audit evidence.
5. `RUNBOOK.md` — setup, operate, recover, rotate credentials, add a fourth source.
6. `KNOWN_LIMITATIONS.md`, `TIME_LOG.md`, `AI_DISCLOSURE.md`.
7. `README.md` — the front page that routes a reviewer to all of the above in
   under a minute, with exact build, start, test and recovery commands.

The diagram can be Mermaid in Markdown; it renders on GitHub and costs minutes,
not an hour. **Reuse** the ADR and decision-log style already established in
`thai-public-data-platform/docs/` — short, states the rejected option explicitly.

---

## Phase 8 — Submit (22:00–22:30)

1. Freeze the code. No new features after 22:00.
2. Verify a clean clone builds: clone to a temp path, confirm `.runtime` is absent,
   confirm fixture hashes match, confirm the compose file references nothing outside
   the repo except `.runtime`.
3. `git log -p | grep -Ei '(bearer|token|BEGIN .*PRIVATE KEY|example\.invalid)'`
   must return nothing meaningful.
4. Push to `https://github.com/Palapluem/siamcodex_data_engineer_interview_test`
   and make it public (or invite the interviewer if it stays private).
5. Submit the Google Form.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Docker Desktop will not start | medium | **fatal** | Check at 12:30, not at 14:00. Mail the interviewer the moment it looks bad. Phase 2 normaliser work is offline and can proceed regardless. |
| Ingestion phase overruns | medium | high | Normalisers are unit-tested offline first, so the network-dependent part is small. Hard stop at 16:15 — move to the API with whatever converges and note the gap. |
| Authorisation subtleties eat time | medium | medium | The matrix is already decided in `BUILD_PROMPT.md` section 6. Implement it as a table and stop debating. |
| Clarification sheet contradicts an assumption | high | low | Both contested semantics (timezone, clearance scope) are env flags, so a change is configuration, not a rewrite. That is the point of documenting them. |
| Docs get squeezed | high | high | Docs start at 20:45 regardless of build state. An undocumented feature scores less than a documented gap. |
| Evaluation uses a different seed | certain | low | Tests assert invariants; seed-specific numbers appear only as labelled evidence. |
| CRLF corrupts fixture hashes | **already hit** | high | Fixed: `.gitattributes` `* -text`, `core.autocrlf=false`, hashes re-verified. |

---

## What carries over from `thai-public-data-platform`

| Existing asset | How it maps here |
|---|---|
| `quality/gate.py`, `quality/schema_contract.py` | Validate against a declared contract; record failures as evidence. Here: quarantine with reason codes instead of a blocking gate, because ingestion must not stop. |
| `public_sources/watermark.py` | Never move a watermark backwards; a replay must be safe. Here: the source cursor, advanced only inside the applying transaction. |
| `sql/postgres/00N_*.sql` | Numbered, idempotent migration files applied at startup. |
| `docker-compose.yml` | Loopback-only publishes, healthchecks, named volumes, no secrets in the image. |
| `docs/adr/`, `docs/DECISIONS.md` | Decision-log style that names the rejected alternative. |
| `pyproject.toml`, `.github/workflows/ci.yml` | ruff + pytest config, `integration` marker, CI shape. |
| Airflow DAGs | **Deliberately not carried over.** Three async pollers on one small server do not need an orchestrator; the decision log explains why. |

---

## Cut-scope ladder

Cut from the top when behind. Never cut from the bottom.

1. `/status` metrics detail beyond the contract's required fields
2. `case_event` lineage table (keep the hash column idea in the design docs)
3. `ROLE=api|worker` split flag
4. Load test shortened from 30 s to 15 s
5. Separate ADR files merged into one `DECISIONS.md`

**Never cut:** version-wins correctness, cursor-in-transaction, the authorisation
matrix with allow and deny tests, restart safety, `ASSUMPTIONS.md`,
`CLARIFYING_QUESTIONS.md`, `AI_DISCLOSURE.md`.
