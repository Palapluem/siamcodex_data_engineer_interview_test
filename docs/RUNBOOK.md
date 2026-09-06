# Runbook

Every command is copy-pasteable and was run on this machine. Paths are relative
to the repository root unless a block says otherwise.

---

## 1. First-time setup

Prerequisites: Docker Engine or Docker Desktop with Compose v2, Python 3.10+,
OpenSSL. Reserve 4 CPU cores, 6 GiB RAM and 8 GiB disk for the whole lab.

```sh
# Windows only: the fixtures are hash-verified and a default clone corrupts them
git config core.autocrlf false

# Confirm the fixtures are byte-identical to their manifest
python candidate/submission/tools/verify_fixtures.py

cd candidate

# 1. Fixed lab: generates local-only credentials and a CA into .runtime
python scripts/bootstrap.py
docker compose -f infrastructure/compose.yaml up -d --build --wait
docker compose -f infrastructure/compose.yaml --profile tools run --rm probe

# 2. Our stack: generates submission/.env with a random database password
python submission/setup_env.py
docker compose -f submission/compose.yaml up -d --build
```

`bootstrap.py` and `setup_env.py` both refuse to overwrite existing state. If
`.runtime` already exists, keep it — do not delete it mid-exercise.

Expected: the probe prints five OK lines, and within ~25 seconds the pipeline has
converged.

```sh
curl -s 127.0.0.1:8088/health
# {"status":"ok"}
```

## 2. Daily operation

```sh
# Operator view: cursors, per-source state, quarantine count
OP=$(python -c "import json;print(json.load(open('candidate/.runtime/client/test_tokens.json'))['operator'])")
curl -s -H "Authorization: Bearer $OP" 127.0.0.1:8088/status | python -m json.tool

# Business view
AN=$(python -c "import json;print(json.load(open('candidate/.runtime/client/test_tokens.json'))['analyst_all'])")
curl -s -H "Authorization: Bearer $AN" '127.0.0.1:8088/reports/daily?unit_id=AST-1' | python -m json.tool

# Logs (structured JSON, redacted)
docker compose -f candidate/submission/compose.yaml logs -f pipeline
```

**Reading `/status`.** `state` is `starting` before the first successful poll,
`healthy` after one, and `degraded` after three consecutive failures.
`last_success_at` is the freshness signal — `/health` is **not**, and neither is
a gateway's `/health`.

## 3. Verifying correctness

```sh
cd candidate/submission

# Offline suite: no Docker, no network
python -m pytest tests -m "not integration"

# Against the running stack
python -m pytest tests -m integration

# Full authorisation matrix with all five caller fixtures
python tools/authz_matrix.py

# Resilience scenarios, writes evidence/scenarios.json
python tools/scenarios.py

# Load test, writes evidence/loadtest.json
python tools/loadtest.py
```

Direct database checks:

```sh
PSQL="docker compose -f candidate/submission/compose.yaml exec -T postgres psql -U pipeline -d meridian"

$PSQL -c "SELECT count(*) FILTER (WHERE NOT is_deleted) active,
                 count(*) FILTER (WHERE is_deleted) deleted,
                 sum(amount_minor) FILTER (WHERE NOT is_deleted) amount
          FROM meridian.case_current;"

$PSQL -c "SELECT unnest(reason_codes) reason, count(*)
          FROM meridian.quarantine GROUP BY 1 ORDER BY 2 DESC;"

$PSQL -c "SELECT at, sub, role, route, decision, reason, http_status
          FROM meridian.access_audit ORDER BY at DESC LIMIT 20;"
```

## 4. Recovery

### The service is down or wedged

```sh
docker compose -f candidate/submission/compose.yaml restart pipeline
```

Volumes are retained. Cursors resume from the database, so nothing is re-counted
and nothing is skipped. Verified: totals identical, cursors unchanged, back in 3 s.

### A source is stuck in `degraded`

1. Check which failure kind it is:
   ```sh
   $PSQL -c "SELECT source, state, last_error_kind, consecutive_failures, last_success_at
             FROM meridian.source_state;"
   ```
2. Interpret `last_error_kind`:

   | Kind | Meaning | Action |
   |---|---|---|
   | `outage` | The gateway reports `link_unavailable` | The link is down. Nothing to do; it recovers automatically. |
   | `transient` | Injected fault or a gateway upstream blip | Self-healing; it retries the same cursor. |
   | `rate_limited` | We hit the source ceiling | Lower that source's budget in `RATE_BUDGET`. Should not happen — we run below the ceiling. |
   | `auth` | 401: the key is wrong or rotated | Replace `source_credentials.json`; the poller reloads it automatically, or restart to force it. |
   | `bad_request` | 400: our query is malformed | A bug. Check `PAGE_LIMIT` is 1–200 and that no extra query parameter was added. |
   | `network` | Timeout, reset or TLS failure | Check the gateway container is up and the CA file is mounted. |

3. Nothing needs restarting for a source to recover. The poller keeps trying
   with backoff, and the other two sources are unaffected.

### The database is gone or corrupt

```sh
docker compose -f candidate/submission/compose.yaml down          # keeps the volume
docker compose -f candidate/submission/compose.yaml up -d
```

If the volume itself is lost, the pipeline rebuilds from the sources. Cold start
to full convergence was measured at **22 seconds**:

```sh
docker compose -f candidate/submission/compose.yaml down -v
docker compose -f candidate/submission/compose.yaml up -d
```

### Force a full replay

Business totals must not change. This is the idempotency check.

```sh
$PSQL -c "UPDATE meridian.source_state SET cursor='0';"
# wait ~15s, then re-check counts and the quarantine count
```

### Recover a quarantined event

There is no automated replay path — see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
To force one event to be re-evaluated after a fix:

```sh
$PSQL -c "DELETE FROM meridian.quarantine WHERE source='birch' AND event_id='birch-e00042';"
$PSQL -c "UPDATE meridian.source_state SET cursor='0' WHERE source='birch';"
```

## 5. Credential rotation

### A source token

```sh
# The interviewer replaces .runtime/client/source_credentials.json, then:
docker compose -f candidate/submission/compose.yaml restart pipeline
```

No restart is strictly required: on a 401 the poller re-reads the mounted file
and picks up the new token by itself, backing off 30 s rather than retry-storming.
A restart just makes it immediate.

### The identity service key

```sh
# Replace .runtime/client/identity_service.key, then:
docker compose -f candidate/submission/compose.yaml restart pipeline
```

This one does require a restart: the key is read once at startup.

### The database password

```sh
cd candidate
rm submission/.env
python submission/setup_env.py
docker compose -f submission/compose.yaml down          # keep the volume
docker compose -f submission/compose.yaml up -d
```

The existing volume keeps the **old** password, so also rotate it inside
Postgres, or recreate the volume and let the pipeline re-ingest (22 s):

```sh
docker compose -f submission/compose.yaml exec -T postgres \
  psql -U pipeline -d meridian -c "ALTER USER pipeline PASSWORD 'new-value';"
```

### Full credential refresh for evaluation

The interviewer provisions fresh credentials. Archive the old runtime rather
than deleting it:

```sh
mv candidate/.runtime candidate/.runtime.bak
cd candidate && python scripts/bootstrap.py
docker compose -f infrastructure/compose.yaml up -d --build --wait
docker compose -f submission/compose.yaml restart pipeline
```

## 6. Adding a fourth source

1. Add its entry to `source_credentials.json` (URL and token).
2. In `app/config.py`: add the source to `SOURCES`, its budget to `RATE_BUDGET`,
   its unit codes to `KNOWN_UNITS`.
3. In `app/ingest/normalize.py`: add the adapter function, its entry in
   `ADAPTERS`, `STATUS_MAPS`, `IDENTIFIER_KEY` and `UNIT_KEY`.
4. Add unit tests for the adapter mirroring `TestVendorMapping`.
5. Rebuild. `seed_source_state` creates the new state row at startup; nothing
   else changes.

Storage, cursor handling, retry, authorisation and reporting are source-agnostic.

## 7. Scenario switches (test harness)

These drive the fixed lab the way the interviewer will. Host-side only.

```sh
cd candidate
python submission/tools/lab_control.py show
python submission/tools/lab_control.py phase 2            # release new events
python submission/tools/lab_control.py outage birch on    # drop one link
python submission/tools/lab_control.py outage birch off
python submission/tools/lab_control.py revoke analyst_all
python submission/tools/lab_control.py unrevoke analyst_all
python submission/tools/lab_control.py transient off      # stop injected 503s
python submission/tools/lab_control.py reset
```

## 8. Shutdown

Stop only this exercise's resources. **Never** run a broad `docker prune`, and
never delete the lab volumes during restart testing.

```sh
cd candidate
docker compose -f submission/compose.yaml down
docker compose -f infrastructure/compose.yaml down
```

## 9. Building the submission archive

```sh
python candidate/submission/tools/make_submission_zip.py
```

It verifies the fixture hashes, refuses to package uncommitted work, scans every
file for credential material, and asserts the finished archive contains no
`.runtime` and no key files.
