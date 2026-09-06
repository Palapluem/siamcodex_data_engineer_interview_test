# Known limitations

Stated plainly, because an undocumented gap reads as an oversight and a
documented one reads as a decision. Each entry says what would fix it.

---

## Correctness edges

**A tombstone arriving before any upsert leaves an identity-only row.**
If a delete at version 2 is delivered before the version 1 upsert, the row is
created with `is_deleted=true` and null attributes, and the later version 1
upsert is correctly ignored because 1 is not greater than 2. The case is absent
from reports and returns 404, which is the right *outcome*, but we never learn
what the case was. It cannot happen with the current fixture (seq order
guarantees v1 first) and it would require genuinely out-of-order delivery.
*Fix:* keep tombstones in a side table and reconcile, rather than materialising
them into `case_current`.

**Equal versions are ignored, not merged.**
An event whose version equals the stored version is dropped. That is what makes
duplicate delivery safe, but if a vendor ever corrected a record *without*
incrementing the version, we would silently miss it. *Fix:* compare payload
hashes on equal versions and quarantine a mismatch as a contract violation.

**No cross-vendor case resolution.**
`C00001` in aster, birch and cobalt are three separate cases, which is correct
per the contract. If the business later decides they are the same underlying
case, we have no master mapping, no survivorship rules and no merge confidence.
*Fix:* a separate mapping table with candidate and approved states — deliberately
out of scope here.

**Single currency.**
Everything is THB. `currency` is stored per case and a missing one is
quarantined, but there is no FX layer and no rate-as-of semantics. Mixed
currencies would be aggregated wrongly rather than refused. *Fix:* refuse to
aggregate across currencies at the query layer first, then add conversion only
if the business needs a single figure.

**Cancelled cases contribute their amount to reports.**
We report what the source says rather than zeroing cancelled value. That is a
business question, not a technical one — clarification question 3.

---

## Operational gaps

**Quarantine is a count, not a workflow.**
`/status` exposes `quarantine_count` and the reasons are queryable, but nothing
alerts, nobody owns the queue, and there is no replay-from-quarantine path. The
runbook documents a manual recovery. *Fix:* an owner, an SLA, and a replay
endpoint — deliberately not built because the brief defines none of the three.

**No alerting.**
A source stuck in `degraded` is visible only to an operator who looks. *Fix:*
scrape `/status` or export Prometheus metrics and alert on `state != healthy`
for longer than a threshold, and on quarantine growth rate.

**Retention is documented but not enforced.**
The windows in [TRUST_BOUNDARIES.md](TRUST_BOUNDARIES.md) have no pruning job.
`case_event` is the table that would breach the 2 GiB ceiling first, at roughly
290 MB per 90 days at 10,000 events/day. *Fix:* a scheduled `DELETE` by
`received_at`, which is also the point at which the "no orchestrator" decision
starts being worth revisiting.

**Metrics live inside `/status` rather than a metrics endpoint.**
Deliberate — an extra open port is an extra thing to secure — but it means no
Prometheus scrape without a shim. *Fix:* a `/metrics` route behind the same
operator policy.

**Audit writes are best-effort.**
A failed `access_audit` insert is logged and swallowed so it can never make a
permitted request fail. For a regulated deployment that trade is wrong: the
audit write should be in the same transaction as the read it authorises.

---

## Security gaps

**Contacts are unencrypted at rest.**
They sit in one column in a container-private volume, protected by access
control rather than cryptography. Anyone with volume or database access reads
them. *Fix:* column-level encryption with a key from a real secret manager, plus
rotation — which needs a decision on whether retaining personal data is required
at all (clarification question 7).

**The database password lives in `submission/.env`.**
Generated randomly and git-ignored, and the exercise's own `.gitignore`
anticipates that file, but it is a file on disk rather than a secret manager.
*Fix:* Docker secrets or an external store in any real deployment.

**No rate limiting or request quotas on our own API.**
A caller with a valid token can issue unlimited report queries. At 65 req/s
Postgres is already near its CPU allocation, so this is a genuine availability
risk. *Fix:* per-subject token bucket in front of the business routes.

**The introspection cache is per-process and in-memory.**
Correct at one replica; with several, each holds its own cache and a revocation
could take up to 5 s per replica. Acceptable within the contract's 5 s bound,
but worth knowing before scaling out.

---

## Testing gaps

**No load beyond 8 concurrent clients or 10,000 events.**
The "no materialised aggregate" decision is justified by measurement at this
scale only. The threshold where it stops holding is estimated, not found.

**No sustained-run evidence.**
The longest continuous run was minutes. Slow leaks, connection-pool churn and
WAL growth over hours are unobserved.

**Sub-second memory spikes cannot be ruled out.**
`docker stats` sampled at 1 Hz. The contract makes this point itself.

**No chaos beyond the provided switches.**
We exercised outages, revocation, restart, replay and identity failure. We did
not test a Postgres kill mid-transaction, disk-full, or a gateway returning
well-formed but wrong data.

---

## Scope deliberately not built

Not gaps — decisions, listed so nobody wonders whether they were forgotten.

- **No UI.** Excluded by the brief.
- **No cloud deployment or enterprise IdP.** Excluded by the brief.
- **No real VPN.** The gateways simulate the constraint;
  [ARCHITECTURE.md](ARCHITECTURE.md) discusses how route collisions, MTU black
  holes and rekey drops would be investigated instead.
- **No fourth source.** The seam is described and the adapter interface exists;
  crossing it is one config entry and one module.
- **No plugin framework or field-mapping DSL.** Three hand-written adapters beat
  a configuration language nobody can debug when it produces wrong money.
- **No historical reporting.** The view is current state. `case_event` proves
  provenance but cannot reproduce a superseded version.
