# Clarification sheet, and how this submission answers it

The interviewer issued a shared Q&A to all candidates covering four requirements,
with a note that the equal-version rule "was not fully explicit in the original
materials" and that decision logs should be updated where earlier assumptions
differ.

**Outcome: all four clarifications match the defaults this submission already
shipped.** Nothing had to be reversed. One implementation gap was found and
fixed, described under Q4.

Source: shared Q&A spreadsheet, reproduced verbatim below so the reasoning stays
traceable without the link.

---

## Q1 — Which timezone defines the business day in `/reports/daily`?

> Use Asia/Bangkok. Derive the report date from the business occurrence timestamp
> converted to that timezone, regardless of the source timestamp's original
> offset. Corrections update the original business day; they do not create new
> activity on ingestion day.
>
> Example: 2026-06-01T18:00:00Z is 2026-06-02T01:00:00+07:00, so it belongs to
> the June 2 report.

**Matches assumption A1.** `BUSINESS_TIMEZONE=Asia/Bangkok`, and every source
timestamp is normalised to UTC at the adapter before the report converts it with
`AT TIME ZONE`. Reports bucket on `event_time`, never on `received_at`, so a
correction lands on the original business day.

This was clarification question 1, raised because the choice moves the report
from 540 rows to 558 on identical data. Confirmed: **558** is correct.

Tests: `tests/unit/test_clarified_rules.py::TestQ1BusinessDay`, including the
sheet's worked example and the same instant expressed in all three vendor
formats.

---

## Q2 — Should cancelled cases contribute to reported value?

> Yes. Sum the value of current, nondeleted cases within each normalized status
> group, including cancelled cases in their own group. Output amount_minor as
> exact integer satang in THB. A cancelled case's value is reported under
> cancelled, not under completed.
>
> Example: a cancelled case worth THB 1,000 contributes one case and 100000 to
> amount_minor in its cancelled group. Deleted cases do not contribute to any
> group. Cancellation and deletion are different concepts.

**Matches assumption A9.** The report groups by `(date, unit_id, status)` and
sums `amount_minor` within each group, filtering only `NOT is_deleted`. Cancelled
is a status, deleted is a lifecycle flag, and they are separate columns —
"cancellation and deletion are different concepts" is enforced structurally
rather than by convention.

Amounts are exact integer satang: birch's decimal strings convert through
`Decimal`, and a value not exactly representable in minor units is quarantined
as `inexact_minor_units` rather than rounded.

---

## Q3 — Does clearance also apply to `/reports/daily`?

> Yes. Apply both unit membership and classification clearance before aggregation.
>
> - internal clearance permits only internal cases.
> - restricted clearance permits both internal and restricted cases.
> - A units claim containing * permits every unit.
> - An omitted unit filter returns only the authorized subset; an explicitly
>   requested unauthorized unit returns 403, even if it has no data.
>
> Analysts with purpose operations may query reports. Auditors with purpose audit
> may query reports and case details, including contacts; the auditor fixture has
> all-unit membership and restricted clearance. Operators may access status but no
> business reports or case details. Other role/purpose combinations are forbidden.
> Reports must not expose contacts or case identifiers.
>
> Example: an analyst with internal clearance must see neither the count nor the
> value of restricted cases in report totals.

**Matches assumptions A2, A3, A4 and A12** — all four of the judgement calls we
flagged as contested.

| Clarified rule | Where it is enforced | Verified by |
|---|---|---|
| Clearance filters aggregates | `routes.py` — `all_classifications` predicate | `analyst_aster` sees 1,180 AST-1 cases; `analyst_all` sees 1,475 |
| `*` permits every unit | `Principal.permitted_units` | `test_authz.py::TestUnitScoping` |
| Omitted filter → authorised subset | `routes.py` | `test_live_contract.py` |
| Unauthorised unit → 403 even with no data | authorisation checked before existence | matrix rows for AST-2 and BIR-1 |
| Analyst + operations may read reports | `ROUTE_ROLES` + `ALLOWED_PURPOSES` | matrix |
| Auditor + audit may read reports **and** case details with contacts | policy table | matrix |
| Operator: status only | policy table | matrix |
| Other role/purpose combinations forbidden | `wrong_purpose` denied everywhere | matrix |
| Reports expose no contacts or case identifiers | row shape is five fields | `test_live_contract.py` |

This was clarification question 2, worth about 20% of reported value. Confirmed:
`CLEARANCE_FILTERS_AGGREGATES=true` is correct, and the flag's other branch is
now dead configuration rather than a live option.

---

## Q4 — What happens when the same business identity has equal versions?

> A business identity is the source plus the vendor case identifier. For an
> existing identity, apply a change only when its version is strictly higher than
> the stored version. A valid first event establishes the state for an unseen
> identity, including when that event is a deletion tombstone.
>
> - A lower or equal version must not overwrite the stored state.
> - A higher seq does not break a version tie.
> - A delete does not take priority over an upsert at an equal version. It must
>   also have a strictly higher version to change an existing state.
> - Retain the deletion version, or equivalent durable protection, so replayed
>   lower or equal versions cannot restore a deleted case.
>
> Example: with an upsert at version 2 already stored, a delete at version 2
> leaves the state unchanged; a delete at version 3 removes the case from reports
> and /cases. After that deletion, an upsert at version 2 or 3 must not restore it.

**Matches assumptions A5, A6 and A7 — with one gap found and fixed.**

| Clarified rule | Status |
|---|---|
| Identity is source + vendor case id | Already: primary key `(source, case_id)` |
| Strictly higher version to change existing state | Already: `WHERE EXCLUDED.version > case_current.version` on **both** the upsert and the delete |
| Lower or equal must not overwrite | Already: same guard |
| A higher seq does not break a version tie | Already: `seq` is not part of the comparison |
| First event may be a tombstone | Already: the delete is `INSERT … ON CONFLICT`, so an unseen identity is created deleted |
| Deletion version retained so replays cannot restore | Already: the delete writes `version = EXCLUDED.version`, so a later upsert at that version or below fails the strict guard |
| **A delete must not take priority over an upsert at an equal version** | **Gap — fixed** |

### The gap

The database guard was right, but the guard alone does not decide the outcome
when *one page* carries both an upsert and a delete at the same version for the
same case. Events were ordered by `(case_id, version)`, so at an equal version
the two tied and kept page order:

- upsert first → upsert lands, delete is then rejected by the strict guard → **alive**
- delete first → delete lands, upsert is then rejected by the same guard → **deleted**

Same input, two outcomes, decided by arrival order. The clarified rule says the
upsert must win, so the outcome must not depend on order at all.

**Fix:** `order_for_apply()` in `app/ingest/apply.py` now sorts by
`(source, case_id, version, is_delete)`. Deletes sort last within a version, so
the upsert always lands first and the delete is always the no-op the rule
requires.

**Impact on the current fixture: none.** No case in seed 73129 carries both an
upsert and a delete at the same version — verified by replaying the fixtures —
so every measured figure in [RESOURCE_REPORT.md](RESOURCE_REPORT.md) is
unchanged. The fix matters for the data released during evaluation, where the
combination is no longer ruled out.

Tests: `tests/unit/test_clarified_rules.py::TestQ4EqualVersionOrdering` and
`::TestQ4VersionGuardIsStrictlyGreater`, covering the ordering, the strictness of
both SQL guards, deletion-version retention, and tombstone-first creation.

---

## Consequences for the rest of the documentation

- [ASSUMPTIONS.md](ASSUMPTIONS.md): A1, A2, A3, A4, A6, A7, A9 move from
  *assumed* to *confirmed*. They stay in the register with their status changed,
  because what we assumed before being told is part of the record.
- [CLARIFYING_QUESTIONS.md](CLARIFYING_QUESTIONS.md): questions 1–4 are answered
  and marked so. Questions 5–9 remain open.
- [DECISIONS.md](DECISIONS.md): D10, D11 and a new D16 record the equal-version
  ordering fix.
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): the "equal versions are ignored"
  entry is no longer a limitation — it is the specified rule. The
  "tombstone before upsert" entry is likewise specified behaviour, not an edge
  we tolerate.
