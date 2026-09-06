# Trust boundaries, data classification and retention

## Boundaries

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ B1  Host loopback 127.0.0.1:8088                                 │
  │     Untrusted callers. Bearer token required on every route but  │
  │     /health. Claims come from introspection, never from headers. │
  └───────────────────────────┬──────────────────────────────────────┘
                              │
  ┌───────────────────────────▼──────────────────────────────────────┐
  │ pipeline container   rootless · read-only rootfs · cap_drop ALL  │
  │                      no-new-privileges · pids 128 · tmpfs /tmp   │
  │                                                                  │
  │  B2 → identity:8443     service key, TLS, verified against CA    │
  │  B3 → vpn-*:8443        per-source bearer, TLS, verified         │
  │  B4 → postgres:5432     private network, generated password      │
  └──────────────────────────────────────────────────────────────────┘
                              │
  ┌───────────────────────────▼──────────────────────────────────────┐
  │ B5  Named volume pipeline_pgdata — the only writable persistence │
  └──────────────────────────────────────────────────────────────────┘
```

| Boundary | Direction | Authentication | What is refused |
|---|---|---|---|
| **B1** | caller → API | Bearer, introspected, cached ≤ 5 s | `X-Role`, `X-Units`, `X-Clearance` and every other client claim; unknown units; any route but `/health` without a credential |
| **B2** | API → identity | `identity_service.key`, TLS to `identity` | Any certificate not signed by the lab CA; any hostname mismatch |
| **B3** | worker → gateway | Per-source bearer, TLS to `vpn-<source>` | Direct access to `source.internal` (unresolvable), certificate or hostname mismatch, plaintext |
| **B4** | app → database | Generated password on a private network | Any connection from outside the compose project; there is no published port |
| **B5** | container → volume | Container-private | Every other filesystem write — the root filesystem is read-only and `/tmp` is a size-capped tmpfs |

### What we deliberately never touch

- `.runtime/server/**` — source keys, the principals file, the TLS private key
- `.runtime/ca.key` — the CA private key
- `.runtime/client/test_tokens.json` — **caller** tokens. They are used by our
  host-side test tooling and are never mounted into the service. A service that
  can read caller tokens can mint its own callers.
- `candidate/data/*.jsonl` — the backing fixtures. Runtime data comes only from
  the APIs, including during replay and recovery.
- The Docker socket, host networking, `extra_hosts`, `NET_ADMIN`, privileged mode.

## Data classification

| Class | Fields | Where it lives | Who may see it |
|---|---|---|---|
| **Restricted — personal** | `contact` | one column in `case_current` | Auditors only, only via `/cases`. Never in a report, a log, an error body or the quarantine table. |
| **Restricted — business** | cases with `classification='restricted'` | `case_current` | Callers with `clearance=restricted`. Internal-clearance callers do not see them in aggregates either (assumption A2). |
| **Internal** | case attributes, aggregates | `case_current`, report responses | Analysts within their permitted units |
| **Operational** | cursors, source state, quarantine counts | `source_state`, `quarantine` | Operators only, via `/status`. Explicitly not business access. |
| **Evidence** | who asked for what and whether it was allowed | `access_audit` | Not exposed by any route; read directly by an operator with database access |
| **Secret** | source tokens, identity service key, CA, database password | mounted files and the environment | The process only. Never logged, never in an image layer, never in git. |

An event whose `classification` is missing or unrecognised is treated as
**restricted**, not rejected: the case still counts, but it is never shown to an
internal-clearance caller. Unknown sensitivity should fail towards silence.

## Access rules

| Route | Rule |
|---|---|
| `/health` | Unauthenticated liveness. No database access, no source state. |
| `/status` | `role=operator` only. Operational visibility is not business access — the brief is explicit that an operator who restarts jobs should not gain data access. |
| `/reports/daily` | `role ∈ {analyst, auditor}` **and** `purpose ∈ {operations, audit}`. Rows are filtered to permitted units, and to `internal` classification unless the caller has `clearance=restricted`. |
| `/cases` | `role=auditor` only. The only route that emits `contact`. |

Evaluation order is fixed: **authenticate → authorise → validate input against an
allowlist → query.** Checking data existence before authorisation would turn a
403 into a 404 oracle that reveals which units hold cases, so an explicitly
requested unauthorised unit returns 403 even when it holds nothing.

Unknown units return 400 rather than an empty result, so an unrecognised input
can never reach the query as an unfiltered predicate.

## Secret handling

| Secret | Provisioned by | Delivered as | Rotation |
|---|---|---|---|
| Source bearer tokens | `scripts/bootstrap.py` | read-only bind mount of `source_credentials.json` | Replace the file, then restart — or let the poller reload it, which it does automatically on a 401 |
| Identity service key | `scripts/bootstrap.py` | read-only bind mount | Replace the file and restart |
| Lab CA certificate | `scripts/bootstrap.py` | read-only bind mount | Replace the file and restart |
| Database password | `submission/setup_env.py` | `submission/.env` → environment | Regenerate `.env`, recreate both containers; see the runbook |

Enforcement:

- Nothing is baked into an image. `.dockerignore` excludes `.env`, and
  `docker history` shows no credential.
- `.gitignore` covers `.runtime/` and `submission/.env`. The exercise's own
  `.gitignore` already anticipated the latter.
- `make_submission_zip.py` scans every packaged file for PEM blocks, bearer
  literals and token-shaped JSON, and refuses to build an archive that contains
  any of them.
- The log formatter redacts contacts, bearer tokens, labelled secrets and bare
  high-entropy strings from **every** record, including exception text this code
  did not write. Tested in `tests/unit/test_redaction.py`.
- The introspection cache is keyed by a SHA-256 of the token, never the token.

## Retention

The exercise is short-lived, but a policy that is not stated is a policy nobody
can audit.

| Data | Retention | Reason |
|---|---|---|
| `case_current` | Indefinite while the case is current | It is the operational truth the report serves |
| `case_event` | 90 days | Enough to investigate a disputed figure; it holds hashes, not payloads |
| `quarantine` | 30 days after resolution | Long enough to diagnose a vendor defect |
| `access_audit` | 1 year | Access evidence outlives the incident that prompts the question |
| Application logs | 14 days | Operational debugging only; they carry no personal data by construction |
| `contact` | Deleted with the case | Personal data lives in exactly one column with one egress route |

Not implemented: automatic pruning. There is no scheduled job enforcing these
windows — see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). Stating the policy
is the deliverable; enforcing it is the follow-up.

## Audit evidence

Every request to a business route writes one `access_audit` row — allow and deny
alike, because a denial is the one you most want a record of. The row carries the
subject claim, role, route, decision, reason, status and any unit filter. It
carries no token and no contact.

```sql
-- Who was denied, and why
SELECT at, sub, role, route, reason, http_status
FROM meridian.access_audit WHERE decision = 'deny' ORDER BY at DESC LIMIT 20;

-- Who has read case detail, the only route that emits contacts
SELECT at, sub, unit_filter FROM meridian.access_audit
WHERE route = 'cases' AND decision = 'allow' ORDER BY at DESC;
```

Auditing never blocks a permitted request: a failed audit write is logged loudly
and swallowed, with the log line as the fallback record. That is a deliberate
availability-over-completeness trade for this exercise, and the wrong trade for
a regulated deployment, where the write should be in the same transaction as the
read it authorises.
