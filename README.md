# Meridian operational view — Siam Codex data engineer assessment

A containerised pipeline that ingests three vendor case systems through
authenticated TLS gateways, resolves them into one trustworthy operational view,
and serves that view over an authorised HTTP API on a single small server.

**Status: complete.** All contract targets met and measured on the hardware named
in [docs/RESOURCE_REPORT.md](docs/RESOURCE_REPORT.md).

| | Measured | Target |
|---|---|---|
| Cold start to convergence | **22 s** | 120 s |
| New events after release | **5 s** | 60 s |
| Recovery after a 30 s outage | **2.1 s** | 60 s |
| `/reports/daily` p95, 8 clients / 30 s | **203 ms** | 500 ms |
| Query errors | **0** of 2,082 | 0 |
| Persistent state | **72 MB** | 2 GiB |
| Tests | **108** passing | — |
| Authorisation matrix | **31/31** | — |

---

## Quick start

Prerequisites: Docker Engine or Docker Desktop with Compose v2, Python 3.10+,
OpenSSL. Reserve 4 CPU cores, 6 GiB RAM and 8 GiB disk for the whole lab.

```sh
git config core.autocrlf false          # Windows: fixtures are hash-verified
python candidate/submission/tools/verify_fixtures.py

cd candidate

# 1. Fixed lab
python scripts/bootstrap.py
docker compose -f infrastructure/compose.yaml up -d --build --wait
docker compose -f infrastructure/compose.yaml --profile tools run --rm probe

# 2. Our pipeline
python submission/setup_env.py
docker compose -f submission/compose.yaml up -d --build

# 3. Check it — converges in about 22 seconds
curl -s 127.0.0.1:8088/health
python submission/tools/authz_matrix.py
```

Verify, then stop only this exercise's resources — never a broad `docker prune`:

```sh
cd candidate/submission
python -m pytest tests -m "not integration"    # offline
python -m pytest tests -m integration          # against the running lab
python tools/scenarios.py                      # outage, restart, replay, revocation
python tools/loadtest.py                       # 8 clients x 30s

cd .. && docker compose -f submission/compose.yaml down
docker compose -f infrastructure/compose.yaml down
```

Full operating detail, recovery and credential rotation: [docs/RUNBOOK.md](docs/RUNBOOK.md).

---

## Where to look

| If you want… | Read |
|---|---|
| The design and a diagram | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Why it is built this way, and what was rejected | [docs/DECISIONS.md](docs/DECISIONS.md) |
| What we assumed, and what breaks if we are wrong | [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) |
| What we would ask the business first | [docs/CLARIFYING_QUESTIONS.md](docs/CLARIFYING_QUESTIONS.md) |
| Trust boundaries, classification, retention, audit | [docs/TRUST_BOUNDARIES.md](docs/TRUST_BOUNDARIES.md) |
| Operating, recovery, rotation, adding a source | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| Measured performance and resource use | [docs/RESOURCE_REPORT.md](docs/RESOURCE_REPORT.md) |
| What is missing and why | [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) |
| Verified facts about the lab and the data | [docs/CONTRACT_FACTS.md](docs/CONTRACT_FACTS.md) |
| Time spent and AI disclosure | [docs/TIME_LOG.md](docs/TIME_LOG.md) · [docs/AI_DISCLOSURE.md](docs/AI_DISCLOSURE.md) |
| How it was planned, before any code | [BUILD_PROMPT.md](BUILD_PROMPT.md) · [docs/PLAN.md](docs/PLAN.md) |
| The problem statement | [candidate/REQUIREMENTS.md](candidate/REQUIREMENTS.md) · [candidate/TECHNICAL_CONTRACT.md](candidate/TECHNICAL_CONTRACT.md) |
| The implementation | [candidate/submission/app/](candidate/submission/app/) |

---

## The shape of the problem

Three acquired businesses, three private connections, three ways of saying the
same thing:

| Meaning | Aster | Birch | Cobalt |
|---|---|---|---|
| case identifier | `case_id` | `ticket` | `ref` |
| unit | `unit_id` | `branch` | `site` |
| event time | ISO-8601 UTC | ISO-8601 `+07:00` | Unix milliseconds |
| value | `amount_minor` int | decimal string, then `net_amount` in v2 | `value_minor` int |
| status | `open/completed/cancelled` | `O/D/X` | `10/20/90` |

10,000 events resolve to **9,000 cases**. Case IDs collide across vendors, the
same `event_id` is delivered twice for 300 of them, corrections arrive with a
higher version, deletes arrive as field-less tombstones, and 100 events are
structurally invalid and must be quarantined without stalling the stream.

## How correctness is achieved

Two rules carry the whole pipeline:

1. **One transaction per page.** The dedupe ledger, lineage, business state,
   quarantine rows and the new cursor all commit together. A failure re-fetches
   the page rather than skipping it. Advancing the cursor outside that
   transaction is the classic silent data-loss bug.
2. **Highest version wins, keyed on `(source, case_id)`.** Equal or lower
   versions are ignored, which makes duplicate delivery, container restart and a
   full replay from cursor 0 all no-ops.

Verified: replaying every source from cursor 0 leaves the totals, the quarantine
count (100) and the idempotency ledger (9,600) unchanged.

## Two findings worth flagging

**The business day changes the answer.** On identical data, UTC produces 540
report rows and Asia/Bangkok produces 558, with different per-day totals. That is
exactly the "two people export the same report and get different answers"
complaint in the brief, so it is clarification question 1 and an environment
variable rather than a silent default.

**Clearance scope is worth about 20% of value.** Whether `clearance` filters
aggregates or only case detail changes `analyst_aster`'s AST-1 view from 1,475
cases to 1,180. Also configurable, also a question rather than a guess.

## Security posture

- Business data comes **only** from the TLS gateways, verified against the lab CA
  by issuer and hostname. The fixtures are never read by the service.
- Only `127.0.0.1:8088` is published. The database has no published port.
- Identity comes from the introspection fixture, cached at most 5 s, failing
  closed. `X-Role`, `X-Units` and `X-Clearance` are ignored — tested.
- Tokens and contacts never reach a log line, an error body or an image layer.
  Redaction is a formatter-level filter, not per-call-site discipline.
- Every business request writes an `access_audit` row, allow and deny alike.
- `.runtime/` and `submission/.env` are git-ignored; the packaging tool refuses
  to build an archive containing credential material.

## Repository layout

```
candidate/              fixed exercise inputs — unchanged, SHA-256 verified
├── data/               synthetic fixtures (inspection only; never read at runtime)
├── infrastructure/     the lab: three sources, three gateways, identity, probe
├── scripts/            bootstrap.py, probe.py
└── submission/         >>> all of our code <<<
    ├── app/            config, db, ingest/, api/, obs/, migrations/
    ├── tests/          unit (offline) + integration (marked)
    └── tools/          authz_matrix, scenarios, loadtest, lab_control, packaging
docs/                   design, decisions, evidence
evidence/               raw scenario and load-test output
```

`candidate/` outside `submission/` is a verbatim copy of
[PMUCxCU/data_engineer_interview_test](https://github.com/PMUCxCU/data_engineer_interview_test).

> **Windows note:** this repository sets `.gitattributes` to `* -text` and requires
> `git config core.autocrlf false`. A default Windows clone rewrites the fixture
> line endings to CRLF, which changes all three SHA-256 values and breaks the
> manifest check.

## AI assistance

Used, and disclosed in full in [docs/AI_DISCLOSURE.md](docs/AI_DISCLOSURE.md),
including three bugs it introduced that I caught and corrected. Every line
submitted can be explained and modified under review.
