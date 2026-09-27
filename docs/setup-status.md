# First-run setup status

`GET /setup/status` is Pi's read-only projection of Conker's first-run workflow. The browser
gateway exposes it at `GET /api/control/pi/setup/status` after owner login. It is an owner-control
route: the conversation runtime credential cannot call it.

The response is versioned with `schemaVersion: 1` and always lists the steps in this order:

1. `security`
2. `companion`
3. `model`
4. `memory`
5. `capabilities`
6. `boundaries`
7. `protection`
8. `rehearsal`

Every state is recomputed from durable configuration or live dependency evidence. The endpoint
does not store progress flags, infer user intent, or turn a UI visit into completion. Persisted
evidence includes the Companion and model-role revisions. Live checks cover the Pi store, the
selected answer-model provider, MemoryGate, and ToolGate's scoped catalogue.

Each step includes typed `prerequisites` and a stable `blockingReasonCode` when work or evidence is
missing. The response selects one eligible `currentStep` whose prerequisites are terminal and one
enum-valued `recommendedNextOperation`. These are bounded operation identities, not URLs, shell
commands, credentials, or permission grants. UI and CLI clients should render these fields rather
than infer workflow behavior from the human-readable evidence details.

Memory and capabilities are optional in this contract. Their absence is `not_started`, while a
configured dependency that cannot be verified is `degraded`. A reachable ToolGate with an empty
scoped catalogue is `in_progress`, not complete.

## External evidence receipts

Pi can inspect a secret-free digest of the ToolGate policy effective for its scoped execution
credential, but it cannot prove host backups or recovery drills or run an assembled-product
rehearsal. The owner control plane may inspect policy and record evidence produced by those
authorities through these narrowly allowlisted routes:

- `GET /setup/boundaries` reads the current scoped policy summary and digest.
- `GET /setup/receipts/{step}` reads the current receipt.
- `POST /setup/receipts/{step}` records a new receipt.
- `{step}` is exactly `boundaries`, `protection`, or `rehearsal`.

The write body is strict JSON:

```json
{
  "receiptId": "rehearsal-release-2026-09-26",
  "source": "conker.release-acceptance",
  "subject": "installation.primary",
  "evidenceDigest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "completedAt": "2026-09-26T12:00:00Z",
  "expiresAt": "2026-10-26T12:00:00Z",
  "expectedRevision": 0
}
```

`evidenceDigest` is the lowercase SHA-256 digest of evidence retained by the named source; Pi does
not accept the evidence document or infer its meaning. `receiptId`, source and subject are bounded
machine identities. Times must include a timezone. Future or already-expired evidence is rejected.
Boundary and rehearsal receipts must expire within 30 days of completion; protection receipts must
expire within 90 days. A boundary receipt must match ToolGate's current digest and becomes stale
immediately if scoped tools, authorization, execution policy, usage limits, scope patterns or
lockdown state change. Unchanged evidence still expires after 30 days. No external receipt can make
a setup step permanently complete. Legacy or malformed stored receipts with a missing expiry or an
expiry beyond the step's maximum validity are projected as stale rather than grandfathered.

Revisions are monotonic per step. `expectedRevision` makes concurrent replacement fail with `409`.
An identical retry of the same `receiptId` is idempotent. Reusing that ID with different content,
or reusing one evidence digest for another step, fails with `409`. The response includes the exact
step, revision, source, subject, digest, completion/expiry/recording times, and a computed `valid` or
`stale` state.

Recording a receipt is an owner attestation about already-produced external evidence. It does not
run a policy review, backup, restore, or rehearsal. A request that merely reaches Pi cannot create
evidence: callers must provide a distinct evidence identity and digest, pass owner authentication,
and satisfy revision and temporal checks. Missing receipts remain `not_started`; expired receipts
are `degraded` with a step-specific stale reason. Only a current receipt completes its step.

Receipts live in the same SQLite database as the rest of Pi state. Startup creates the additive
table for older databases, and ordinary SQLite backup/restore preserves the full append history.

This response is Pi's contribution to the larger control-plane workflow. Its `security` evidence
proves only that Pi's durable store is readable and the distinct owner-control channel is
provisioned. It does not prove that the gateway owner password has been set; the gateway must add
that evidence before the product-level security step can be considered complete.

The top-level `state` is derived from required steps. A blocked required step wins, then degraded
evidence, then completion; otherwise the workflow is in progress. Timestamps describe
when the projection was generated and are not completion receipts. For identical persisted and
live evidence, the complete response is restart-stable except for `generatedAt`.
