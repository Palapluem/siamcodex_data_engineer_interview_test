# Clarifying questions

The brief grades the **quality and prioritisation** of questions, not the number.
These are ordered by how much the answer changes the implementation. Each one is
already implemented behind a documented default, so an answer is a configuration
change, not a rewrite.

Send questions 1–5 early; 6–9 can wait for the review.

---

## Blocking — the numbers differ depending on the answer

### 1. What is the business day?

`event_time` describes the business occurrence. Aster sends UTC, Birch sends
`+07:00`, Cobalt sends Unix milliseconds — all three encode the same instant, so
the wire format is not the question. The question is which calendar a regional
manager means by "daily".

Measured impact on the current fixture:

| Business timezone | non-empty rows | distinct dates | last date |
|---|---|---|---|
| UTC | 540 | 30 | 2026-06-30 |
| Asia/Bangkok | 558 | 31 | 2026-07-01 |

Grand totals are identical; per-day rows are not. This is literally the
"two people export the same report and get different answers" complaint.

**Our default:** `Asia/Bangkok`, configured via `BUSINESS_TIMEZONE`. The
cooperative is Thai, all money is THB, and Birch already reports in `+07:00`.

**If the answer is UTC** we flip one environment variable. **If different regions
have different business days**, that is a bigger change — it makes the day a
property of the unit, not of the deployment, and we would want to know now.

### 2. Does `clearance` filter aggregates, or only case detail?

The brief says sensitive cases need tighter handling than routine summary
information. That reads two ways, and they give different totals for the same
analyst:

| Interpretation | AST-1 cases | sum amount_minor |
|---|---|---|
| Clearance gates case detail only; aggregates cover all cases in permitted units | 1,475 | 112,767,732 |
| Clearance also filters aggregates to `internal` | 1,180 | 90,216,320 |

**Our default:** clearance filters aggregates too — an `internal` caller sees only
`classification='internal'` cases. Rationale: it makes `clearance` mean something
consistent on both routes, and under-disclosure is the safer failure. Configured
via `RESTRICTED_IN_AGGREGATES`.

**Risk we want checked:** this means two analysts can legitimately see different
totals for the same unit. If leadership wants one number per unit regardless of
who asks, the answer is interpretation 1 and we flip the flag.

### 3. What does "service value" mean for cancelled work?

`/reports/daily` returns `amount_minor` per `(date, unit, status)`, so cancelled
cases carry a value. Should a cancelled case contribute to regional "service
value", or is the amount retained only for reconciliation?

**Our default:** report the amount for every status as stored, and let the consumer
decide. We do not zero cancelled amounts, because silently dropping them would make
the API disagree with the source systems.

### 4. Should auditors see `/reports/daily`?

The contract fixes `/status` as operator-only and `/cases` as auditor-only, but is
silent on reports for auditors.

**Our default:** yes — `purpose=audit` is a legitimate business purpose and the
auditor holds `units=["*"]` and `clearance=restricted`. Denying it would make the
assurance team unable to reconcile a case against the aggregate it appears in.

### 5. Which purposes are allowed for business data?

The `wrong_purpose` fixture is an analyst with `purpose=marketing` and otherwise
full scope, so it plainly exists to be denied.

**Our default:** an allowlist of `{operations, audit}`; everything else is 403.
Please confirm the intended list rather than an implicit "not marketing".

---

## Important — affects design, not today's numbers

### 6. What is the freshness expectation in production?

The contract fixes evaluation targets (120 s baseline, 60 s for new events), but
those are exercise targets. What does the operations team actually need — near
real time, hourly, or next business day? It decides whether continuous polling is
right or whether a scheduled batch with a much smaller resource footprint would
serve better.

**Our default:** continuous polling with a 3 s idle interval, which meets the
stated targets with headroom.

### 7. How much history must be reconstructible, and for whom?

We keep current state plus an append-only event ledger with payload **hashes**, not
payloads. That supports "trace where this case came from" but not "show me exactly
what the vendor sent on 3 June".

**Our default retention:** `case_current` indefinite, `case_event` 90 days,
`quarantine` 30 days after resolution, `access_audit` 1 year, logs 14 days.

Two things we want confirmed: is a hash enough for the assurance team, and does any
regulation require retaining the original vendor payload including contact details?
That second answer changes the data-classification design, because it would mean
storing restricted personal data for a defined period rather than the minimum.

### 8. Who owns a quarantined event, and what is the SLA?

100 events in the current fixture are structurally invalid. We quarantine them with
reason codes, keep ingesting, and expose the count on `/status`. But nothing in the
brief says who investigates them or how quickly.

**Our default:** operators see the count; there is no alerting or replay-from-
quarantine workflow in this submission. It stays a documented proposal.

### 9. Should a case that disappears from a source be treated as deleted?

Deletes arrive as explicit tombstones today. If a vendor instead stops sending a
case, we will never know it is gone.

**Our default:** only explicit tombstones delete. We do not infer deletion from
absence, because on an append-only cursor stream absence is indistinguishable from
"not yet delivered".

---

## Questions we deliberately are **not** asking

Stating these is part of the answer — the brief warns against assuming every
unspecified feature is required.

- **No UI.** The brief excludes it.
- **No enterprise IdP.** The identity fixture is the identity provider.
- **No real VPN.** The gateways simulate the constraint; we discuss route-table
  collisions, MTU and UDP tunnels in `ARCHITECTURE.md` rather than building them.
- **No multi-currency.** Everything is THB today. We store `currency` per case and
  refuse to aggregate mixed currencies, but we build no FX layer.
- **No fourth source today.** We describe the migration path and where we stop
  generalising, and we do not build a plugin framework nobody asked for.
