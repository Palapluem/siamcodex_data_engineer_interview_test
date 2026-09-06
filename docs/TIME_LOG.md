# Time log

Assessment window: 12:00–23:59, 6 September 2026. Work began at 12:05.
Commit timestamps are in the git history and corroborate each phase boundary.

| Phase | Time | Duration | Output |
|---|---|---|---|
| 0a — Understand the problem | 12:05–12:28 | 23 min | Read the brief, contract and lab README. Read `server.py` and `generate_data.py` rather than trusting the illustrative payload. Replayed the fixtures offline to compute the expected converged state. Wrote `CONTRACT_FACTS.md`, `PLAN.md`, `ASSUMPTIONS.md`, `CLARIFYING_QUESTIONS.md`. |
| 0b — Lab up | 12:28–12:42 | 14 min | `bootstrap.py`, lab healthy, probe green. Confirmed the live payloads against the documented field mapping with `peek_source.py`. Built the submission packaging tool. |
| 1 — Skeleton | 12:42–12:48 | 6 min | Compose, Dockerfile, pinned deps, config, migration, `/health`. |
| 2 — Ingestion | 12:48–12:53 | 5 min | Normalisers with unit tests first, then client, transactional apply, pollers. Converged on all three sources. |
| 3 — Result API | 12:53–12:59 | 6 min | Introspection with a 5 s cache, policy table, `/status`, `/reports/daily`, `/cases`. Full authorisation matrix green. |
| 5 — Resilience | 12:59–13:04 | 5 min | `lab_control.py`, `scenarios.py`. Phase-2 release, outage, revocation, restart, replay, identity failure. |
| 4 + 6 — Tests and measurement | 13:04–13:09 | 5 min | 108 tests. Load test, resource sampling, cold-start and storage measurement. |
| 7 — Documentation | 13:09–13:45 | 36 min | Architecture and diagram, trust boundaries, decision log, runbook, resource report, known limitations, this log, AI disclosure, README. |
| 8 — Submission | 13:45– | — | Clean-clone verification, archive, form. |

**Total: approximately 1 hour 45 minutes** of focused work.

## On the time

The brief budgets 10–14 hours. This took under two, and the honest explanation
matters more than the number.

**What made it fast.** Nearly all of it was the first 23 minutes. Reading the
fixed infrastructure's source produced the exact rate limits, the injected
failure at every cursor divisible by 600, the phase gating and the vendor field
mapping — so no time was spent discovering those by trial. Replaying the
fixtures offline produced the expected converged state *before* any pipeline
code existed, which turned "does this work?" into a single comparison against
known numbers. Every phase after that had a defined target and a way to check it
in one query.

The design also stayed small on purpose. Two containers, no orchestrator, no
message broker, no materialised aggregate — each of those was a decision not to
build something, and each is defended in [DECISIONS.md](DECISIONS.md) with the
measurement or the reasoning that made it the right size for one small server.

**Where the leverage was, concretely.** Writing the normalisers with unit tests
before touching the network caught two real bugs offline: `Decimal.to_integral_exact()`
silently rounding money because the default context has the `Inexact` trap
disabled, and a naive timestamp being assumed UTC rather than rejected. Both
would have been far harder to spot as a wrong total in a converged pipeline.

**AI assistance is disclosed** in [AI_DISCLOSURE.md](AI_DISCLOSURE.md) and is a
real part of why this was fast. It does not change the claim I am making: I can
explain and modify every line, and the design decisions and their tradeoffs are
mine to defend.

**What I would do with the remaining hours.** In priority order: prove the
fourth-source seam by actually adding one; add alerting and an owner for
quarantine; run a longer load sweep to find where the plain `GROUP BY` stops
being the right answer rather than asserting a threshold; and implement the
retention pruning that is currently documented but not enforced. Those are in
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md), and I would rather ship a complete,
measured core path with honest gaps than a broader system I cannot evidence.
