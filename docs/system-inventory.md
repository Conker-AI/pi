# Owner system inventory

Pi exposes schema-versioned owner-browser routes using its separate owner credential
and existing scoped ToolGate client:

- `POST /system/inventory` with a stable `request_id` and optional `limit` (1–200).
- `GET /system/inventory/{request_id}` to inspect the original ToolGate receipt.
- `POST /system/inventory/{request_id}/resume` to submit its saved approval ID.
- `GET /system/inventory/configured/services` and `/configured/containers` for
  authoritative configured-target metadata. These fixed reads are explicitly
  `observed: false` and do not claim that a service or container exists or is healthy.

The browser allowlist accepts only these exact paths and canonical request IDs. It
does not expose query-selected hosts, paths, PIDs or target kinds, a generic proxy,
the older system mutation routes, filesystem inventory, terminal access, or Docker
socket access. The conversation runtime credential cannot use this contract.

Pi has no new SystemGate credential, outbound destination, shell or host API.
ToolGate's `system.inventory` reserved executor identity provides the fixed read
operation. This requires ToolGate's inventory adapter and identity guard; it is
not a fallback to generic HTTP or an arbitrary tool selected by the browser.
ToolGate scope, owner policy, revocation and receipts remain authoritative.

Before dispatch, Pi records the request identity and exact limit. Concurrent or
replayed POSTs return that record without dispatching again. After a lost reply,
GET reads the action receipt; it never repeats the read invocation. A saved
approval is resumed explicitly and atomically claimed. Restart marks unfinished
dispatches unknown and leaves reconciliation to receipt inspection. Request IDs
cannot be reused with different limits. A new sample requires a new ID.

The first successful POST or a successful receipt GET returns a strict version `1`
projection plus `currentAgeSeconds`, calculated from its original sample time.
Partial status is retained. Replayed POSTs only return the local request state;
use GET for the saved observation. If a previously completed receipt becomes
unavailable, its known completion is preserved but `observation=null` and
`receiptStatus=unavailable`; Pi does not fabricate a fresh or empty observation.

Browser observations carry `authority: none`, `contentIncluded`, and
`execution: read-only-observation`. Process PIDs and ToolGate lifecycle IDs become
stable HMAC-backed `process_...` identifiers. Container and port identities are
similarly opaque. The HMAC key is generated during Pi store initialization, survives
restart/backup, and is never returned; inventory reads do not create or rotate it.
Process command lines, users, restart controls and raw PIDs are omitted. Container
image names and raw daemon IDs are omitted. Host addresses become only `loopback`,
`all-interfaces`, or `specific`; procfs paths and unavailable-field labels are not
returned. Port links refer only to the opaque process/container identities in the
same bounded observation.

Section status, truncation and allowlisted machine error codes remain explicit.
Configured-target responses omit action lists and container IDs, declare
`execution: not-triggered`, and return 503 when the dependency is unavailable rather
than presenting an empty configured system. Runtime services are not observed by
the current SystemGate envelope; the service endpoint is configuration metadata only.

One cross-gate limitation remains outside Pi: the current SystemGate runtime emits
`configured-container-source` / `unavailable` container scopes and the explicit
`container_telemetry_not_configured` / `container_bindings_not_configured` errors,
while ToolGate's current inventory envelope accepts only `configured-docker-daemon`
and omits those two error codes. Pi accepts both bounded vocabularies, but ToolGate
must reconcile that contract before those SystemGate partial samples can reach Pi.
Until then the read fails unavailable/failed; Pi never substitutes an empty sample.

Pi retains request metadata and approval references, not another copy of host
observations. The originating ToolGate receipt retains the result under its own
retention. Missing configuration is 503; invalid receipts become failed reads.
Approval IDs, raw receipts, service exception text and credentials are not surfaced.
This endpoint cannot control processes, containers, port mappings, files or terminals.

Tests use temporary SQLite, a synthetic ToolGate client/HTTP response, concurrent
requests, approval/resume, restart recovery, stable opaque identities, malformed
row rejection, configured-target failures, redaction and exact owner authentication.
They do not inspect or change a real machine.
