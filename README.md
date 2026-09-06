# Meridian operational view — Siam Codex data engineer assessment

A containerised data pipeline that ingests three vendor case systems through
authenticated TLS gateways, resolves them into one trustworthy operational view,
and serves that view over an authorised HTTP API on a single small server.

**Status: planning complete, implementation in progress.**
See [docs/PLAN.md](docs/PLAN.md) for the schedule and current phase.

---

## Where to look

| If you want… | Read |
|---|---|
| The problem statement | [candidate/REQUIREMENTS.md](candidate/REQUIREMENTS.md) · [candidate/TECHNICAL_CONTRACT.md](candidate/TECHNICAL_CONTRACT.md) |
| What is being built and why | [BUILD_PROMPT.md](BUILD_PROMPT.md) |
| Verified facts about the lab, the data and the expected results | [docs/CONTRACT_FACTS.md](docs/CONTRACT_FACTS.md) |
| The plan and its risks | [docs/PLAN.md](docs/PLAN.md) |
| What we assumed and what breaks if we are wrong | [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) |
| What we would ask the business first | [docs/CLARIFYING_QUESTIONS.md](docs/CLARIFYING_QUESTIONS.md) |
| Architecture, trust boundaries, decisions, runbook, measurements | `docs/` *(written in phase 7)* |
| The implementation | `candidate/submission/` |

---

## Repository layout

```
candidate/              fixed exercise inputs — unchanged, SHA-256 verified
├── data/               synthetic fixtures (inspection only; never read by the service)
├── infrastructure/     the lab: three sources, three gateways, identity, probe
├── scripts/            bootstrap.py, probe.py
└── submission/         >>> all of our code lives here <<<
docs/                   design, decisions, evidence
BUILD_PROMPT.md         the full build specification
```

`candidate/` is a verbatim copy of
[PMUCxCU/data_engineer_interview_test](https://github.com/PMUCxCU/data_engineer_interview_test).
Nothing outside `candidate/submission/` has been modified — verify with:

```sh
python candidate/submission/tools/verify_fixtures.py
```

> **Windows note:** this repository sets `.gitattributes` to `* -text` and requires
> `git config core.autocrlf false`. A default Windows clone rewrites the fixture
> line endings to CRLF, which changes all three SHA-256 values and breaks the
> manifest check the interviewer runs.

---

## Quick start

Prerequisites: Docker Engine or Docker Desktop with Compose v2, Python 3.10+,
OpenSSL. Reserve 4 CPU cores, 6 GiB RAM and 8 GiB disk for the whole lab.

```sh
# 1. Fixed lab — generates local-only credentials into candidate/.runtime
cd candidate
python scripts/bootstrap.py
docker compose -f infrastructure/compose.yaml up -d --build --wait
docker compose -f infrastructure/compose.yaml --profile tools run --rm probe

# 2. Our pipeline
docker compose -f submission/compose.yaml up -d --build

# 3. Check it
curl -s 127.0.0.1:8088/health
```

Stop only this exercise's resources — never a broad `docker prune`, and never
delete the volumes during restart testing:

```sh
docker compose -f submission/compose.yaml down
docker compose -f infrastructure/compose.yaml down
```

---

## The shape of the problem

Three acquired businesses, three private connections, three different ways of
saying the same thing:

| Meaning | Aster | Birch | Cobalt |
|---|---|---|---|
| case identifier | `case_id` | `ticket` | `ref` |
| unit | `unit_id` | `branch` | `site` |
| event time | ISO-8601 UTC | ISO-8601 `+07:00` | Unix milliseconds |
| value | `amount_minor` int | decimal string, then `net_amount` in v2 | `value_minor` int |
| status | `open/completed/cancelled` | `O/D/X` | `10/20/90` |

10,000 events resolve to **9,000 cases** — case IDs collide across vendors, the
same `event_id` can be delivered twice, corrections arrive with a higher version,
deletes arrive as field-less tombstones, and 100 events are structurally invalid
and must be quarantined without stalling the stream.

Full derivation, including the expected converged state used as acceptance
criteria, is in [docs/CONTRACT_FACTS.md](docs/CONTRACT_FACTS.md).

---

## Security posture

- Business data is obtained **only** through the TLS gateways, verified against the
  lab CA by issuer and hostname. Fixtures are never read by the service.
- Only `127.0.0.1:8088` is published. The database has no published port.
- Identity comes from the introspection fixture, cached for at most 5 seconds, and
  fails closed. Client-supplied role or unit headers are ignored.
- Tokens and contacts never reach a log line, an error body or an image layer.
- `.runtime/` is git-ignored; the interviewer provisions fresh credentials.

Trust boundaries, data classification and retention: `docs/TRUST_BOUNDARIES.md`
*(phase 7)*.

---

## AI assistance

Disclosed in [docs/SUBMISSION_FORM_ANSWERS.md](docs/SUBMISSION_FORM_ANSWERS.md)
and `docs/AI_DISCLOSURE.md`. Every line submitted can be explained and modified
under review.
