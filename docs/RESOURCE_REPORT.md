# Resource and performance report

Every number below is **measured** on the hardware named here unless explicitly
labelled *estimate*. Reproduction commands are given for each. Raw output is in
[`evidence/`](../evidence).

> Sampling does not prove the absence of brief spikes. `docker stats` was polled
> at 1 Hz, so a sub-second memory excursion between samples would not appear.
> Where that matters it is called out.

---

## Measured environment

| | |
|---|---|
| CPU | Intel Core i5-9400F @ 2.90 GHz, 6 cores / 6 threads |
| RAM available to Docker | 7.73 GiB |
| Host OS | Windows 11 Pro 26100 |
| Docker | Engine 29.7.2, Compose v5.5.0, Docker Desktop (WSL2 backend) |
| Date | 6 September 2026 |
| Dataset | 10,000 events across three sources, seed 73129, phase 2 released |
| Converged state | 9,000 distinct cases · 8,850 active · 150 tombstoned · 100 quarantined |

The whole lab (7 fixture containers at 128 MiB each) plus our 2 containers runs
inside this budget with room to spare.

---

## Allocations, and why

| Service | `cpus` | `mem_limit` | Peak measured | Headroom | Rationale |
|---|---|---|---|---|---|
| `pipeline` | 1.0 | 512 MiB | 63.2 MiB · 62% CPU | 8× memory | Three async pollers plus FastAPI. Memory sits near 60 MiB; the limit is 8× that deliberately — Python's allocator releases lazily, a burst of concurrent report queries buys transient row buffers, and 1 Hz sampling cannot rule out a spike. 512 MiB costs nothing here and turns a possible OOM-kill into a non-event. |
| `postgres` | 0.75 | 512 MiB | 61.9 MiB · 82% CPU | 8× memory | `shared_buffers=128MB`, `max_connections=30`. The 14 MB database fits entirely in cache. CPU is the real constraint, not memory — see below. |

Also set on `pipeline`: `read_only: true`, `cap_drop: [ALL]`,
`no-new-privileges`, `pids_limit: 128`, `tmpfs /tmp` capped at 64 MiB, non-root
user 65532.

`postgres` does **not** get `read_only`: initdb and the WAL need a writable data
directory. Its capability set is dropped to the five initdb actually requires
(`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`), and it publishes no port.

**Honest note on right-sizing.** Measured peaks say both services would fit in
128 MiB. I kept 512 MiB because the cost is zero on this host and the failure
mode of being wrong is an OOM-kill mid-transaction. On a genuinely constrained
server I would drop both to 192 MiB and watch, which is a config change.

---

## Timing

| Target | Contract | Measured | Method |
|---|---|---|---|
| Startup to convergence | ≤ 120 s | **22 s** | `down -v` then `up -d`, polled until 8,850/150 |
| New events visible | ≤ 60 s | **5 s** | `lab_control.py phase 2`, polled until converged |
| Recovery after a 30 s outage | ≤ 60 s | **2.1 s** | `scenarios.py outage` |
| Restart to serving | — | **3.0 s** | `scenarios.py restart` |
| Full replay from cursor 0 | — | **14 s** | `scenarios.py replay` |

Cold-start progression, from an empty volume:

```
t+11s  2,400 cases     t+19s  8,700 / 100 deleted
t+15s  7,000 cases     t+22s  8,850 / 150      converged
```

The floor is set by cobalt: 17 pages at a 1.5/s client budget plus five injected
503 retries. Reproduce:

```sh
docker compose -f candidate/submission/compose.yaml down -v
docker compose -f candidate/submission/compose.yaml up -d
```

---

## Query performance

`/reports/daily` as `analyst_all` (all six units, restricted clearance — the
widest query the API can serve), 8 concurrent clients, 30 seconds.

```sh
python candidate/submission/tools/loadtest.py --clients 8 --seconds 30
```

| Metric | Measured | Target |
|---|---|---|
| p50 | 103.5 ms | — |
| **p95** | **203.2 ms** | ≤ 500 ms |
| p99 | 293.4 ms | — |
| max | 1,735 ms | — |
| mean | 116.5 ms | — |
| Throughput | 65.0 req/s | — |
| Requests | 2,082 | — |
| **Errors** | **0** | 0 |

Sampled during the same run:

| Service | CPU mean | CPU peak | Mem mean | Mem peak |
|---|---|---|---|---|
| `pipeline` | 46.4% | 61.8% | 54.6 MiB | 63.2 MiB |
| `postgres` | 72.5% | 81.5% | 60.5 MiB | 61.9 MiB |

**Reading these numbers honestly:**

- **Postgres is the bottleneck, not the app.** At 72.5% mean of its 0.75 CPU
  allocation it is close to its ceiling while the pipeline sits at 46%. The
  aggregate is a full scan of 8,850 rows per request at 65 req/s. Raising
  `postgres` to 1.0 CPU is the first lever if throughput ever needs to grow;
  materialising the aggregate is the second, and neither is needed to meet the
  target.
- **The 1,735 ms maximum is real and worth naming.** It is a single outlier
  early in the run — first-request connection-pool warm-up coinciding with a
  poller commit. It does not recur: p99 is 293 ms. A production deployment would
  warm the pool at startup.
- **This is the widest query.** A scoped caller (`analyst_aster`, one unit,
  internal only) reads 1,180 rows instead of 8,850, so real-world p95 will be
  lower than the figure above.

---

## Storage

| | Measured | Limit |
|---|---|---|
| Named volume `pipeline_pgdata` | **71.9 MB** | 2 GiB |
| Database `meridian` | 14 MB | — |
| Application image | 229 MB | (not persistent state) |

Per table:

| Table | Size | Rows |
|---|---|---|
| `case_event` | 3,008 kB | 9,600 lineage records |
| `case_current` | 1,920 kB | 9,000 cases |
| `event_seen` | 1,464 kB | 9,600 idempotency keys |
| `quarantine` | 96 kB | 100 |
| `access_audit` | 96 kB | grows with traffic |
| `source_state` | 64 kB | 3 |

Most of the 72 MB volume is empty WAL and the Postgres cluster's own baseline,
not our data. **Growth estimate (not measured):** `case_event` is the only table
that grows without bound, at roughly 320 bytes per event. At 10,000 events per
day the 90-day retention window in
[TRUST_BOUNDARIES.md](TRUST_BOUNDARIES.md) implies about 290 MB — comfortably
inside 2 GiB, but it is the table that would breach it first, and pruning is
currently unimplemented.

---

## Stability

| | Measured |
|---|---|
| Container restarts during all testing | **0** for both services |
| Query errors under load | 0 of 2,082 |
| Injected 503s encountered and recovered | 15 (5 per source, as predicted) |
| Auth, 400 or malformed-response failures | 0 |
| Quarantine count across a full replay | 100 → 100 (unchanged) |
| `event_seen` across a full replay | 9,600 → 9,600 (unchanged) |

`event_seen` at 9,600 is the arithmetic working out: 10,000 events, minus 100
quarantined before they reach the ledger, minus 300 duplicate deliveries that
collapse onto their existing `(source, event_id)`.

---

## Correctness evidence

Predicted offline by replaying the fixtures before any code was written
([CONTRACT_FACTS.md](CONTRACT_FACTS.md)), then confirmed against the live API.

| Metric | Predicted | Measured |
|---|---|---|
| Active cases (phase 1) | 9,000 | 9,000 |
| Sum `amount_minor` (phase 1) | 681,046,850 | 681,046,850 |
| Report rows, Asia/Bangkok (phase 1) | 557 | 557 |
| Report rows, UTC (phase 1) | 540 | 540 |
| Active / tombstoned (phase 2) | 8,850 / 150 | 8,850 / 150 |
| Sum `amount_minor` (phase 2) | 670,152,982 | 670,152,982 |
| Report rows, Asia/Bangkok (phase 2) | 558 | 558 |
| Version distribution | 8,400 v1 + 600 v2 | 8,400 v1 + 600 v2 |
| Status split | 3,223 / 2,801 / 2,826 | 3,223 / 2,801 / 2,826 |
| Quarantined pairs | 100 | 100 |

These figures are specific to **seed 73129**. The evaluation may use a different
seed, so the automated tests assert invariants — no duplicate business keys,
tombstones absent from reports, `sum(case_count)` equal to the active count,
totals unchanged across replay — rather than these totals.

---

## What was not measured

- Behaviour beyond 8 concurrent clients, or with a dataset larger than 10,000
  events. Both would be worth knowing before committing to no materialised
  aggregate at a larger scale.
- Sub-second memory excursions. 1 Hz sampling cannot rule them out.
- Sustained multi-hour operation. The longest continuous run here was minutes.
- Build time, which the contract times separately. The image builds in roughly
  40 s warm, dominated by the pip install layer.
