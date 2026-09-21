# Owner system inventory

Pi exposes authenticated owner routes using its existing scoped ToolGate client:

- `POST /system/inventory` with a stable `request_id` and optional `limit` (1–200).
- `GET /system/inventory/{request_id}` to inspect the original ToolGate receipt.
- `POST /system/inventory/{request_id}/resume` to submit its saved approval ID.

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

The first successful POST or a successful receipt GET returns the observed
inventory plus `currentAgeSeconds`, calculated from its original sample time.
Partial status is retained. Replayed POSTs only return the local request state;
use GET for the saved observation. If a previously completed receipt becomes
unavailable, its known completion is preserved but `inventory=null` and
`receiptStatus=unavailable`; Pi does not fabricate a fresh or empty observation.

Pi retains request metadata and approval references, not another copy of host
observations. The originating ToolGate receipt retains the result under its own
retention. Missing configuration is 503; invalid receipts become failed reads.
Service exception text and credentials are not surfaced. This endpoint does not
control processes, containers, port mappings, files or terminals. Frontend wiring
and live service activation remain deferred.

Tests use temporary SQLite, a synthetic ToolGate client/HTTP response, concurrent
requests, approval/resume, restart recovery, invalid receipts and owner route
authentication. They do not inspect or change a real machine.
