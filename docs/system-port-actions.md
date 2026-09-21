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
