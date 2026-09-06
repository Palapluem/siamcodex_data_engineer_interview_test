# Contract facts — verified ground truth

Everything here was derived by reading the fixed exercise inputs
(`candidate/infrastructure/server.py`, `candidate/infrastructure/generate_data.py`)
and by replaying `candidate/data/*.jsonl` offline. It is reference material for
implementation and for writing test assertions. **Nothing in this file may be
read by the running service** — the service must obtain business data only
through the gateway APIs.

Fixture seed: **73129**. A different seed may be used at evaluation with the
same documented semantics, so treat exact counts as *evidence for our documented
run*, and treat the structural invariants as the real test assertions.

---

## 1. Mock source API behaviour (from `server.py`)

| Behaviour | Detail | Implication for us |
|---|---|---|
| Endpoint | `GET https://vpn-<source>:8443/v1/changes?cursor=<int>&limit=<1..200>` | Gateway proxies the exact path to `source.internal:8080`, forwarding `Authorization`. |
| Query validation | Strict. Any key other than `cursor`/`limit`, a repeated key, `cursor<0`, or `limit` outside 1–200 → **400**. | Build the query string exactly; never add tracing params. |
| Paging | Returns events with `seq > cursor`, capped at `limit`. `next_cursor` = last `seq` in the page, or the **unchanged cursor** for an empty page. `has_more` = any visible event beyond `next_cursor`. | Cursor is a source-local watermark. Persist it; never fabricate a forward cursor. |
| Rate limit | Sliding 1s window, **source-wide** (shared across all connections): aster **8/s**, birch **4/s**, cobalt **2/s**. Exceeding → **429** + `Retry-After: 1`. | Client-side token bucket *below* the ceiling. Do not parallelise within one source. |
| Minimum latency | aster 50 ms, birch 100 ms, cobalt 150 ms per request, applied after the rate check. | Floor on convergence time; see section 5. |
| Injected transient fault | While `transient_errors` is on, the **first** request whose `cursor > 0 and cursor % 600 == 0` returns **503** + `Retry-After: 1`, once per cursor value per process lifetime. | With `limit=200` cursors land on 600/1200/1800/2400/3000 → **5 forced 503s per source**. This is a deliberate retry test — handle it, never dodge it by choosing a limit that misses the multiples. |
| Outage switch | `control.json` → `outages[source] = true` → **503** `link_unavailable` + `Retry-After: 2`. | Must isolate: the other two sources keep polling normally. |
| Auth | Bearer must equal that source key exactly (`hmac.compare_digest`), else **401**. | 401 means rotated or wrong key. Do not retry-storm; surface as degraded and reload credentials. |
| Gateway upstream | 5 s timeout; upstream failure → **503** + `Retry-After: 1`. | Client read timeout must exceed 5 s (use 10 s). |
| `/health` | Unauthenticated liveness only. | Explicitly **not** evidence of freshness or authorization. |
| Phase gating | Only events with `phase <= control.phase` are visible. Phase starts at **1**. | The interviewer flips to phase 2 to release new events mid-run. |
| Control file | `.runtime/server/control/control.json`, padded to 4096 bytes, re-read per request with a bounded retry. Keys: `phase`, `outages`, `revoked_tokens`, `transient_errors`. | Our scenario tooling must write it atomically and preserve the padding. |

### Identity fixture

`POST https://identity:8443/introspect`, header `Authorization: Bearer <identity_service.key>`,
body `{"token": "<caller bearer>"}`.

- Known, non-revoked token → `200 {"active": true, "sub", "role", "units", "clearance", "purpose"}`
- Unknown or revoked token → `200 {"active": false}` — **not** a 4xx, so check the flag, not the status code
- Body must be 1–4096 bytes, else **400**
- Control file unavailable → **503** + `Retry-After: 1`

Provisioned principals (`candidate/scripts/bootstrap.py`):

| Fixture | sub | role | units | clearance | purpose |
|---|---|---|---|---|---|
| `analyst_aster` | u-aster | analyst | `["AST-1"]` | internal | operations |
| `analyst_all` | u-all | analyst | `["*"]` | restricted | operations |
| `auditor` | u-audit | auditor | `["*"]` | restricted | audit |
| `operator` | u-operator | operator | `[]` | internal | operations |
| `wrong_purpose` | u-purpose | analyst | `["*"]` | restricted | **marketing** |

TLS: one certificate, `CN=Interview Gateway`, SAN `DNS:vpn-aster, vpn-birch, vpn-cobalt, identity`,
signed by the lab CA at `.runtime/client/ca.crt`. Verify issuer **and** hostname.

---

## 2. Vendor field mapping

| Canonical | Aster | Birch | Cobalt |
|---|---|---|---|
| case_id | `case_id` | `ticket` | `ref` |
| unit_id | `unit_id` | `branch` | `site` |
| event_time | `event_time` ISO-8601 (UTC) | `occurred_at` ISO-8601 (**+07:00**) | `timestamp_ms` Unix ms |
| amount | `amount_minor` **int** | v1 `amount` decimal **string** (major units); v2 `net_amount`, with `amount` **removed** | `value_minor` **int** |
| status | `open` / `completed` / `cancelled` | `O` / `D` / `X` | `10` / `20` / `90` |

Shared fields: `op` (`upsert`/`delete`), `version`, `schema_version`, `classification`
(`internal`/`restricted`), `contact`, `currency` (all THB in the current fixture).

Money rule: birch amounts are decimal strings in major units. Convert with
`Decimal`, never a binary float: `int((Decimal(raw) * 100).to_integral_exact())`.
A non-exact result is a validation failure, not a rounding opportunity.

Units allowlist (fixed): `AST-1, AST-2, BIR-1, BIR-2, COB-1, COB-2`.

---

## 3. Fixture composition (seed 73129)

10,000 events across three sources. An event is **not** a case.

| Source | events | phase 1 | phase 2 | upserts | deletes | schema v2 | duplicate deliveries |
|---|---|---|---|---|---|---|---|
| aster | 3334 | 3134 | 200 | 3284 | 50 | 0 | 100 |
| birch | 3333 | 3133 | 200 | 3283 | 50 | **150** | 100 |
| cobalt | 3333 | 3133 | 200 | 3283 | 50 | 0 | 100 |

Each source carries 3,000 distinct cases `C00001`–`C03000`. **Case IDs deliberately
collide across vendors** — business identity is `(source, case_id)`, never `case_id` alone.

Deterministic case bands, identical in all three sources:

| Band | What happens | What it tests |
|---|---|---|
| `C00001`–`C00100` | The same `event_id` is delivered **twice at different `seq`** | Idempotent apply / dedupe |
| `C00101`–`C00250` | Phase-2 correction at `version 2` (amount +1250 minor, status → completed; birch also moves to `net_amount` and `schema_version 2`) | Update semantics, schema evolution |
| `C00251`–`C00300` | Phase-2 **tombstone** at `version 2` — identity and version only, every other field absent | Delete semantics |
| `C00301`–`C03000` | Single `version 1` upsert | Baseline |

Plus **100 deliberately invalid events** (34 aster / 33 birch / 33 cobalt), all phase 1:

```json
{"op":"upsert","version":1,"schema_version":1,"case_id":"INVALID-<i>","amount_minor":"broken","event_time":"not-a-time"}
```

The identifier key is always `case_id`, so for birch and cobalt the business
identifier is *missing entirely*. These must be quarantined without stalling ingestion.

---

## 4. Expected converged state

Computed by replaying the fixtures with the intended semantics: dedupe by
`(source, event_id)`, highest `version` wins, tombstones excluded from reporting.

### Phase 1 — baseline, what the reviewer sees after startup

| Metric | Value |
|---|---|
| distinct business keys | 9,000 |
| active (non-deleted) cases | 9,000 |
| tombstoned | 0 |
| quarantined `(source, event_id)` pairs | **100** |
| version distribution | all v1 |
| status: completed / cancelled / open | 2,980 / 2,984 / 3,036 |
| sum of `amount_minor` | 681,046,850 |
| `/reports/daily` non-empty rows | **557** (Asia/Bangkok) · 540 (UTC) |

### Phase 2 — after the interviewer releases the remainder

| Metric | Value |
|---|---|
| distinct business keys | 9,000 |
| active cases | **8,850** |
| tombstoned | **150** |
| quarantined pairs | **100** — unchanged, because replays must not inflate it |
| version distribution | 8,400 × v1, 600 × v2 |
| status: completed / cancelled / open | 3,223 / 2,801 / 2,826 |
| sum of `amount_minor` | 670,152,982 |
| `/reports/daily` non-empty rows | **558** (Asia/Bangkok) · 540 (UTC) |
| active cases per unit | 1,475 each, across 6 units |
| restricted / internal (active) | 1,770 / 7,080 |

### The timezone decision is load-bearing

Same data, different business-day definition:

| Business timezone | non-empty rows | distinct dates | date range |
|---|---|---|---|
| UTC | 540 | 30 | 2026-06-01 … 2026-06-30 |
| Asia/Bangkok (+07) | 558 | 31 | 2026-06-01 … **2026-07-01** |

Row counts and per-day totals differ; the grand total does not. This is precisely
the "two people export the same report and get different answers" problem from the
brief, so it becomes clarification question 1 and must be a configured, documented
choice — not an accident of casting a timestamp to a date.

### Scoping arithmetic (phase 2, Asia/Bangkok)

| Caller view | cases | rows | sum amount_minor |
|---|---|---|---|
| all units, all classifications | 8,850 | 558 | 670,152,982 |
| all units, internal only | 7,080 | 557 | 534,884,109 |
| AST-1 only (unit filter alone) | 1,475 | 93 | 112,767,732 |
| AST-1 only **and** internal only | 1,180 | 93 | 90,216,320 |

The gap between the last two rows is the cost of clarification question 2:
does `clearance` filter aggregates, or only case detail?

---

## 5. Convergence budget

Pages needed at `limit=200`: ceil(3334 / 200) = 17 per source, plus 5 forced 503
retries and one final empty page — roughly 23 requests per source.

| Source | ceiling | our client budget | requests | time floor |
|---|---|---|---|---|
| aster | 8/s | 6/s | ~23 | ~4 s |
| birch | 4/s | 3/s | ~23 | ~8 s |
| cobalt | 2/s | 1.5/s | ~23 | ~15 s |

Sources poll concurrently, so the baseline floor is set by cobalt at roughly
15–20 s against the 120 s target. Comfortable margin, and it still holds if we
halve the client-side budget.

---

## 6. Hard prohibitions (from README and TECHNICAL_CONTRACT)

- Do not read, mount, copy or bake in `candidate/data/*.jsonl` from the service.
- Do not modify `infrastructure/`, `data/`, `scripts/`, the README or the contract.
- Do not publish anything except `127.0.0.1:8088`; never publish the database.
- Do not join `*_private` networks, use host networking, `extra_hosts`, `NET_ADMIN`,
  privileged mode, or the Docker socket.
- Do not disable TLS verification and do not bypass a gateway.
- Do not mount `test_tokens.json` into the service — those tokens belong to API callers.
  Never touch `.runtime/server` or the CA private key.
- Do not trust `X-Role`, `X-Units` or any other client-supplied claim.
- Do not log tokens or contacts, including inside error messages.
- Do not commit `.runtime`; the interviewer provisions fresh credentials.
- Never run broad `docker prune`; never delete lab volumes during restart testing.

---

## 7. Windows landmine, already handled

`git config core.autocrlf` is `true` on this machine. A normal clone rewrites every
fixture line ending to CRLF, which changes all three SHA-256 values and breaks the
manifest check the interviewer runs. This repo ships `.gitattributes` with `* -text`
and `core.autocrlf=false`, and the copied fixtures were re-cloned with LF preserved.
Verified: all three hashes match `candidate/data/manifest.json`.

Re-verify at any time:

```sh
python - <<'PY'
import json, hashlib, pathlib
d = pathlib.Path("candidate/data")
m = json.loads((d / "manifest.json").read_text())
for s, meta in m["sources"].items():
    h = hashlib.sha256((d / f"{s}.jsonl").read_bytes()).hexdigest()
    print(s, "OK" if h == meta["sha256"] else "MISMATCH")
PY
```
