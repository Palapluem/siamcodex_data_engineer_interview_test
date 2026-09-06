# Assumption register

Every assumption we implemented, why it matters, what happens if the clarification
sheet says otherwise, and the switch that changes it. Assumptions marked
**configurable** are environment variables, so a different answer costs a restart,
not a rewrite. See [CLARIFYING_QUESTIONS.md](CLARIFYING_QUESTIONS.md) for the
questions behind the contested ones.

| # | Assumption | Why we chose it | If wrong | Change cost |
|---|---|---|---|---|
| A1 | Business day is **Asia/Bangkok** | Thai cooperative, THB throughout, Birch already reports `+07:00` | 540 report rows instead of 558; every per-day figure shifts | **configurable** — `BUSINESS_TIMEZONE` |
| A2 | `clearance` filters **aggregates as well as** case detail | Makes clearance mean one thing on both routes; under-disclosure is the safer failure | An `internal` analyst under-reports their unit by ~20% of value | **configurable** — `RESTRICTED_IN_AGGREGATES` |
| A3 | Allowed purposes for business data are `{operations, audit}` | The `wrong_purpose` fixture exists to be denied | A denied caller should have been allowed | **configurable** — policy table |
| A4 | Auditors may read `/reports/daily` | Contract fixes only `/status` and `/cases`; audit is a legitimate purpose | One row of the policy table flips | **configurable** — policy table |
| A5 | Business identity is `(source, case_id)` | Contract says so explicitly; fixture case IDs collide across all three vendors | Cross-vendor merging would need a master mapping and a survivorship rule | structural — would need a new identity resolution layer |
| A6 | Highest `version` wins; equal or lower is ignored | Contract: "corrections have higher version values" | Late-arriving corrections at equal version would be dropped | small — one predicate in the upsert |
| A7 | Tombstones retain previously known attributes and set `is_deleted` | Reports must exclude them, `/cases` must 404, but lineage should survive | If deletes must purge, a delete becomes a physical delete plus an audit row | small |
| A8 | Amounts are exact minor units; a non-exact conversion is a validation failure | THB has 2 decimals; the fixture generator explicitly warns against binary floats | Rounding policy would need defining with the business | small |
| A9 | Cancelled cases keep their amount in reports | Zeroing them would make the API disagree with the source systems | One `CASE` expression in the report query | small |
| A10 | Only explicit tombstones delete; absence never implies deletion | On an append-only cursor stream, absence is indistinguishable from not-yet-delivered | Would need per-source full-snapshot reconciliation | structural |
| A11 | Continuous polling at a 3 s idle interval | Meets the 60 s new-event target with headroom at negligible cost | A scheduled batch would use less CPU but miss the target | **configurable** — `POLL_IDLE_SECONDS` |
| A12 | The known-unit allowlist is exactly the six fixture units | Contract enumerates them; an allowlist prevents unknown input widening a query | A fourth source adds units — one config list, plus a credentials entry | **configurable** — unit list |
| A13 | Introspection cached for 5 s, keyed by a token **hash** | Contract caps the cache at 5 s; hashing keeps tokens out of memory dumps and logs | Shorter TTL costs latency, not correctness | **configurable** — `INTROSPECT_CACHE_SECONDS` |
| A14 | Identity unreachable → business routes **fail closed** (503) | Contract requires failing closed once a valid cache entry expires | None; this is mandated | fixed |
| A15 | `/health` needs no auth and no database round-trip | Contract: liveness only, and a health check that queries the DB reports the wrong thing | None | fixed |
| A16 | We store `contact` in one column, egressed only on the auditor route | Contract: auditors are the only role with contact access | If contacts must be encrypted at rest or tokenised, add column encryption plus key management | medium |
| A17 | Event lineage stores a payload **SHA-256**, not the payload | Keeps restricted data out of a second table while still proving provenance | If the assurance team needs the original payload, retention and classification both change | medium |
| A18 | One process runs the API and all three pollers | Right-sized for one small server; `ROLE=api\|worker` keeps the split available | Splitting is a compose change, not a code change | **configurable** — `ROLE` |
| A19 | No materialised aggregate | 8,850 rows; a plain `GROUP BY` is far inside the 500 ms p95 target | At roughly 100× the data we would revisit; the report documents the measurement | small |
| A20 | All money is THB; mixed currencies are refused, not converted | Contract: "All current fixture money is THB" | A real FX layer with rate-as-of semantics would be needed | structural |
| A21 | The evaluation seed may differ from 73129 | The data README says so | Tests assert invariants, so nothing breaks; only the labelled evidence numbers change | none |
| A22 | Quarantined events are surfaced as a count, not routed to a workflow | Nothing in the brief defines an owner or an SLA | A replay-from-quarantine path becomes real work | medium — stays a proposal |

## Assumptions we refused to make

- That an unspecified feature is required. No UI, no cloud, no enterprise IdP, no
  real VPN, no fourth source, no alerting stack.
- That `/health` on a gateway proves a source is fresh or that we are authorised.
  The contract says the opposite, and `/status` carries freshness instead.
- That an event equals a case. 10,000 events resolve to 9,000 cases.
- That `case_id` is globally unique. It is not, deliberately.
