# Integration and evaluation contract

## Network boundaries

The fixed Compose project is de-interview-lab. Its external attachment point is the internal Docker bridge named de-interview-transit. Your worker or application may join that network. Three separate private networks each contain a source with the alias source.internal and one gateway. Only the matching gateway is attached to both that private network and transit.

```text
Candidate API / worker / storage (candidate-owned network)
          |
          +-- de-interview-transit (internal)
                |-- vpn-aster:8443 -- aster_private -- source.internal:8080
                |-- vpn-birch:8443 -- birch_private -- source.internal:8080
                |-- vpn-cobalt:8443 -- cobalt_private -- source.internal:8080
                +-- identity:8443
```

This is a VPN constraint simulation using application gateways, not an encrypted VPN implementation. It tests endpoint scoping, private DNS, separate trust and credentials, timeouts, retries and failure isolation. It does not emulate real route-table collisions, MTU problems, UDP tunnels or VPN handshakes. Discuss how you would investigate those in production. Source-private HTTP is a lab simplification; client-to-gateway and client-to-identity traffic uses TLS.

You may create private application networks and publish only the result API on 127.0.0.1:8088. Do not publish databases or join supplied private source networks. Runtime business dependencies must stay within this local lab. Do not add routes, NET_ADMIN, extra_hosts bypasses, host PID/network namespaces or Docker API access. Do not override supplied services with an additional Compose file. The interviewer verifies the merged configuration and running containers as well as file hashes.

## Source API

For each of aster, birch and cobalt use https://vpn-SOURCE:8443/v1/changes. Load each source's distinct bearer credential from the provisioned source_credentials.json. Trust the supplied ca.crt; verify both issuer and hostname. Certificates and tokens are random local test credentials, not production credentials.

GET /v1/changes?cursor=0&limit=100 returns JSON:

```json
{"source":"aster","items":[{"seq":1,"event_id":"aster-e00001","payload":{}}],"next_cursor":"1","has_more":true}
```

The payload above is illustrative; inspect a real first page. cursor is a source-local sequence position, initially the string "0". Request events strictly after that position. limit is 1-200. Keep polling after has_more=false because new events may become visible. An empty page leaves next_cursor unchanged. The stream is append-only; prior pages remain replayable. Do not invent a future cursor to skip ahead.

An event_id identifies an event within its source. Delivery can include the same event_id again at another seq. Business identity is source plus the vendor case identifier. Corrections have higher version values. Deletes are tombstones with identity and version; they may omit other fields. Event time describes the business occurrence, not ingestion order. All current fixture money is THB. Use precise decimal or integer minor-unit arithmetic.

Vendor representations differ:

| Meaning | Aster | Birch | Cobalt |
|---|---|---|---|
| Case identifier | case_id | ticket | ref |
| Unit | unit_id | branch | site |
| Event time | event_time ISO 8601 | occurred_at ISO 8601 | timestamp_ms Unix milliseconds |
| Value | amount_minor integer | amount decimal string in v1; net_amount in v2 | value_minor integer |
| Status | open / completed / cancelled | O / D / X | 10 / 20 / 90 |

Shared payload fields include op, version, schema_version, classification and contact. Units are AST-1, AST-2, BIR-1, BIR-2, COB-1 and COB-2. Classification is internal or restricted. Invalid events must remain diagnosable without stopping all ingestion. Additional data is released by the interviewer during evaluation. Source-wide request ceilings are Aster 8/s, Birch 4/s and Cobalt 2/s, with minimum response delays of 50/100/150 ms respectively. A 429 or transient 503 includes Retry-After in seconds. Expect 401 for a wrong or rotated key. GET /health is unauthenticated liveness only and does not establish source freshness or authorization.

## Identity fixture

Your service calls POST https://identity:8443/introspect with Authorization: Bearer <identity_service.key> and JSON {"token":"<caller bearer token>"}. Valid tokens return active, sub, role, units, clearance and purpose. Invalid or revoked tokens return {"active":false}. Do not trust client-supplied role, unit, clearance or purpose headers. Identity claims come from the fixture, not from decoding an opaque token or loading caller test_tokens.json into the service.

Five caller fixtures are provisioned in .runtime/client/test_tokens.json: analyst_aster, analyst_all, auditor, operator and wrong_purpose. The interviewer uses them as clients. Introspection may be cached for at most five seconds; fail closed for business queries when identity cannot be verified after any valid cache entry expires. Do not log tokens or contacts. Redact sensitive values in error messages too.

## Result API

Listen on container port 8080 and publish 127.0.0.1:8088. All endpoints below return JSON. Except /health, they require an authenticated caller. Make query output stable and sort results as described. Extra top-level metadata is allowed. Do not include extra fields inside row/item objects because they can leak data. Unknown inputs must not turn into unfiltered queries.

GET /health returns 200 for liveness. GET /status is operator-only and returns {"sources":{"aster":{"cursor":"0","state":"starting","last_success_at":null},"birch":{...},"cobalt":{...}},"quarantine_count":0}. Allowed states: starting, healthy, degraded. last_success_at is an ISO 8601 UTC timestamp for the latest successful poll. Quarantine count means distinct rejected source/event_id pairs. Do not return raw payloads or credentials. Status is operational information; it does not grant business-data access.

GET /reports/daily?unit_id=AST-1 or GET /reports/daily returns {"rows":[{"date":"2026-06-01","unit_id":"AST-1","status":"completed","case_count":12,"amount_minor":12345}]}. Sort by date, unit_id, status. Return one row per nonempty group and no zero-filled groups. Counts and values cover current, nondeleted cases; each case appears once at its latest version. Business-day timezone, value meaning and access rules are finalized in the clarification sheet. Aggregate only rows the caller is permitted to see. An explicitly requested unauthorized unit must return 403, including when it has no data. An omitted unit filter returns the authorized subset. Unknown units return 400.

GET /cases?source=aster&case_id=C00001 returns {"item":{...}}. A successful item has exactly these fields: source, case_id, unit_id, version, event_time (UTC ISO 8601), amount_minor, currency, status, classification, contact. Non-auditors get 403 for this route. Auditors get 404 for an unknown or deleted case, and 200 for an existing case. Reject missing or unknown source parameters with 400. Auditors are the only role with contact access in this exercise.

Missing, invalid or revoked caller credentials return 401. A valid caller forbidden by role, purpose, unit or clearance gets 403 for a forbidden operation. Do not accept client claims such as X-Role or X-Units. Report responses must never contain contact, case identifiers or raw payloads.

## Reproducible evaluation

The interviewer supplies the business clarification sheet before implementation scoring. Baseline ingestion should converge within 120 seconds after your application starts against a healthy running lab. New released events should converge within 60 seconds. During a 30-second outage, healthy sources must continue polling; after restoration, catch up within 60 seconds. Target report-query p95 is at most 500 ms at eight concurrent clients over 30 seconds on the agreed host. These are initial exercise targets, not calibrated claims about a particular machine; the interviewer will pilot and freeze them before sending the test.

Choose and document CPU and RAM allocations for every running service in your submission project, including storage and sidecars. Use explicit per-service limits appropriate to your implementation; there is no fixed aggregate CPU or RAM cap. Persist application state in named volumes with total contents at most 2 GiB. Builds are timed separately. Report service startup to convergence, update convergence, response p50/p95/p99, query errors, sampled memory and CPU, container restarts and storage size. A restart with retained volumes must preserve correctness and catch up. Replays must not inflate business counts or quarantine counts.

Performance only counts after correctness and access checks pass. The interviewer will review process and container evidence; sampling does not prove the absence of brief memory spikes. Optional larger-scale experiments are discussed separately and do not silently change the scored workload. Reproduce your tests and clearly label measurements versus estimates.
