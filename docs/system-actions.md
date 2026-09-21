# Owner managed system actions

Pi's owner-only API records exact container lifecycle requests and delegates to
ToolGate's reserved `system.container-control` capability. Pi never opens a Docker
socket or gains SystemGate write credentials.

- `POST /system/actions`: `request_id` (16–100 ASCII letters/digits/underscore/hyphen),
  full lowercase 64-hex `container_id`, and `action` (`start`, `stop`, `restart`).
- `GET /system/actions?limit=50`: latest local request metadata, bounded to 100;
  reading history never calls or approves a tool.
- `GET /system/actions/{request_id}`: inspect the original ToolGate receipt.
- `POST /system/actions/{request_id}/resume`: submit the exact saved approval ID.

ToolGate owns scope, allowlist, current authority, confirmation and execution.
Pi's initial POST asks ToolGate to prepare this exact operation; it does not grant
approval. Existing owner Inbox decisions remain the approval route. Resume does
not approve anything: an undecided, expired, rejected or consumed approval can be
refused by ToolGate. Frontend/gateway wiring, including operation-bound password
verification, is deferred to the separately reviewed integration phase.

Pi reserves the immutable request identity before contacting ToolGate. Reusing it
with different target/action fails; identical or concurrent POSTs only inspect
local state. Concurrent resumes claim the saved approval once. Startup marks
interrupted dispatches unknown and never executes them. GET reconciliation reads
the original action receipt, never the container's current state to guess whether
a restart happened. Lost initial approval responses can remain unknown if no
execution receipt exists; this API does not recreate approval requests on retry.

States are dispatching, awaiting_approval, unknown, complete and failed. A
malformed success receipt remains unknown because the effect may have happened.
Known gate refusals produce failed state and static errors; raw diagnostics and
credentials are not returned. Completion means ToolGate returned the original
bounded observation, not that the container will stay healthy. A later failed
receipt read preserves known completion with `receiptStatus=unavailable`.

Pi stores target/action metadata and approval references; ToolGate retains the
effect receipt. Returned before/after states include only status and booleans;
config, environment, mounts and other Docker fields are excluded. Replayed POSTs
return metadata; GET fetches the saved observation. History is bounded latest-first,
not a complete paginated audit export. Individual known request IDs stay readable.

Requires ToolGate's fixed executor and configured managed-target allowlist. No
daemon or keys are configured automatically. Process controls, port-mapping edits,
terminal and files remain separate backend work. Tests use temporary SQLite and
synthetic gate responses; no real containers or service deployment are exercised.

## Managed target discovery

`GET /system/targets` is owner-only and reads ToolGate's scoped configuration
catalogue with no effect dispatch or local persistence. The transport rejects
redirects/compressed responses, bounds the response to 200 KB and 2,000 unique
full IDs, and validates the fixed lifecycle/approval contract. A checked 10-second
deadline can overrun by an in-progress 5-second read timeout. Failure is unavailable,
never an empty successful inventory; valid unconfigured/disabled states remain
explicit. Both service responses use `Cache-Control: no-store`.

`observed=false` is mandatory. IDs are operator-configured targets, not evidence
of container existence, running state or daemon health. SystemGate's observed
inventory may describe a different daemon; consumers must not infer an identity
match solely from configuration discovery or enable arbitrary process actions.
All actual dispatches still recheck ToolGate's current authority and allowlist.
