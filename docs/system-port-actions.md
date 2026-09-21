# Owner port-change interface

Pi exposes owner-authenticated `POST /system/port-reviews` and
`GET /system/port-reviews/{review_id}` using the existing ToolGate connection and
execution key. Review creation forwards container_id, operation (create/edit/remove),
mapping and/or original as appropriate. This is metadata-only preparation, not
execution or approval. Responses are no-store; Pi does not retain private Docker
configuration, environment values or a second encrypted specification.

Transport is fixed to ToolGate's review routes, does not follow redirects, bounds
responses at 256 KiB and checks a 15-second deadline with 10-second HTTP timeouts.
Projection validates exact requested container/operation/mapping delta, IDs,
replacement flags and expiry. Unknown/debug fields are removed. Errors are static.
The checked deadline is not a hard wall-clock cancellation guarantee.

`POST /system/actions/ports` accepts a stable request_id, full container_id and
review_id. It reuses the existing system action ledger, with a disjoint
`ports:<review_id>` internal action identity; old lifecycle rows need no migration.
Shared history, receipt inspection, saved-approval resume and restart uncertainty
apply. ToolGate owns owner verification and effects. A request ID cannot change
its target or review. The observer validates replacement/snapshot identities,
retained-original status and bounded port mappings, and drops extra data. A malformed
success remains unknown rather than pretending the action failed without effects.

40 focused port-review, port-action, container-action and service-action tests pass
against temporary stores and synthetic transports, including actual ToolGateClient
credentials/paths, concurrent request reservation, original approval resume, no-op
receipts and mismatched upstream output. Scoped lint/diff pass; an existing
Starlette test-client deprecation warning remains.

No real Docker daemon or host resources were used. This is backend-only: the
dashboard remains on fixtures. ToolGate retained-container recovery, replacement
allowlist lineage, review retention and real Docker validation remain unfinished.

## Read-only recovery inspection

`GET /system/actions/{request_id}/recovery` now uses the saved port action identity
to read ToolGate recovery evidence. The originating ToolGate actor remains the
existing connection identity. Pi validates action/container IDs, ordered steps,
references, states, observation time and bounded port mappings, strips unknown
fields and returns no-store metadata. Claims of resumability/release are rejected:
this endpoint does not authorize corrective actions or alter Pi's action ledger.

Review and recovery calls share the bounded transport/parser; both use only their
fixed ToolGate routes. Recovery uses GET, never tool invocation. Unconfirmed new
container identities remain distinct from missing containers or failed reads.

40 recovery/review/port-action/container-action checks passed using temporary stores
and synthetic transport. Scoped lint/diff pass, with the existing test-client
deprecation warning. ToolGate has since implemented managed replacement lineage;
corrective recovery/finalization, retention and real Docker verification remain.
# Verified receipt recovery

POST `/system/actions/{identity}/recovery/finalize` accepts an optional
`approval_request_id`. The owner-authenticated route calls ToolGate's fixed
finalization endpoint for the original saved port action. A new recovery approval
is returned separately and remains discoverable in ToolGate's durable owner Inbox;
it does not replace the original execution approval. After owner approval, submit
that ID to the same route. Pi verifies the action identity, target, completion and
replacement receipt before updating the original ledger entry. Lost responses leave
the action available for receipt inspection; no Docker operation is dispatched.
Only fully verified ToolGate replacements qualify. Partial-effect correction and
frontend wiring remain separate work. All response projections omit extra fields.
