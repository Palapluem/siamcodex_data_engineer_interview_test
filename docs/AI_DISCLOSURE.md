# AI assistance disclosure

The brief asks for this to be disclosed, and the assessment expects me to explain
and modify everything submitted. Both are true here.

## What was used

Claude Code (Claude Opus 5), working in this repository with shell, file and
Docker access, throughout the session.

## What it did

**Analysis of the fixed inputs.** Read `candidate/infrastructure/server.py` and
`generate_data.py` and extracted the actual API behaviour — the source-wide
sliding-window rate limits, the 503 injected at the first request for every
cursor divisible by 600, the phase gating, the strict query-parameter validation,
the identity fixture's `{"active": false}` response for revoked tokens — into
[CONTRACT_FACTS.md](CONTRACT_FACTS.md).

**Offline ground truth.** Wrote and ran a script that replays the 10,000 fixture
events with the intended semantics and computes the expected converged state:
9,000 distinct cases, 8,850 active after tombstones, 100 quarantined,
681,046,850 then 670,152,982 in total value, and the report row counts under
both candidate timezones. This became the acceptance criteria before any
pipeline code was written.

**Planning documents.** Drafted [PLAN.md](PLAN.md), [BUILD_PROMPT.md](../BUILD_PROMPT.md),
and the first versions of [ASSUMPTIONS.md](ASSUMPTIONS.md) and
[CLARIFYING_QUESTIONS.md](CLARIFYING_QUESTIONS.md).

**Implementation.** Wrote the application code, tests and tooling under
`candidate/submission/`, and the documentation in `docs/`.

**Verification.** Ran the lab, the test suite, the authorisation matrix, the
resilience scenarios and the load test, and reported the measurements.

## What I own

The architecture and every decision in [DECISIONS.md](DECISIONS.md): PostgreSQL
over SQLite, one process over a split worker, no orchestrator, no materialised
aggregate, quarantine over fail-fast, cursor-inside-the-transaction. The
authorisation model, including the judgement calls the contract leaves open —
that auditors may read reports, that `{operations, audit}` are the allowed
purposes, that clearance filters aggregates. The two clarifying questions I
consider blocking, and why. Which gaps were acceptable to ship and which were
not.

I reviewed every line before committing, and I can change any of it under
discussion.

## Concrete value, and concrete correction

**Value.** The offline replay is what surfaced that the business-day timezone
changes the report from 540 rows to 558 on identical data. That went into the
clarification list as question 1 and became a configuration flag, instead of
becoming a silently wrong default that two people would later discover by
disagreeing about a number.

**Corrections I made during the work:**

1. The first money conversion used `Decimal.to_integral_exact()`, on the
   assumption that it raises when a value is not exactly representable in minor
   units. It does not — it only *signals* `Inexact`, and the default decimal
   context has that trap disabled, so `143.6449` silently became `14364`. A unit
   test written for that exact case caught it. The code now compares against
   `to_integral_value()` explicitly, and the reasoning is in a comment so nobody
   re-introduces it.

2. The log redaction filter was inspecting its own regex source strings to decide
   which substitution to apply — fragile and unreadable, and it missed the JSON
   form `"api_key": "..."` entirely. Rewritten as explicit ordered rules with a
   test per shape.

3. The submission packager's secret scanner matched its own source, because the
   file contained the literal `-----BEGIN CERTIFICATE-----` as a pattern. The
   easy fix was to exempt the file by name, which would have left the scanner
   itself unscanned. The markers are now assembled from fragments instead.

4. A Windows-specific landmine: `core.autocrlf=true` rewrote every fixture line
   ending on clone, changing all three SHA-256 values and breaking the manifest
   check the interviewer runs. Caught by verifying hashes before starting;
   the repository now pins `.gitattributes` to `* -text`.

## What was not delegated

Judgement about scope. The decision not to build a plugin framework, not to add
an orchestrator, not to materialise the aggregate, and to stop generalising at
three hand-written adapters — those are the decisions this assessment is really
testing, and they are argued from measurements taken on this machine rather than
from a template.
