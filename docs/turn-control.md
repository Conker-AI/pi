# Ordinary conversation execution controls

Owner-only `POST /turns/{turn_id}/cancel` records an idempotent durable stop
request for a running, non-forgotten turn. The response distinguishes requested
cancellation from the worker's current state. Submission reads expose
`cancel_requested`; repeat submissions inspect the receipt without restarting.

Provider and tool-dispatch checkpoints observe the stop. Final reply persistence
checks it in the same writer transaction, so cancellation and completion have a
defined order. A discarded model answer leaves `cancelled`; the initiating HTTP
turn request returns that state instead of a retryable provider failure.

Cancellation is cooperative: it does not terminate an already executing provider
or remote action. Completed action receipts remain recorded as `acted_no_reply`.
Unknown action outcomes remain unknown; explicit resume may check their receipts,
but the stop guard prevents new dispatch and narration. It does not revoke a
separate ToolGate approval or undo effects. Cancellation cannot change an already
finished turn. Call-specific stop/privacy guards continue to apply.

Remaining P9 work: cancellation during submission preparation, explicit consent
for reply-only recovery after a stop, queued future turns, steering, and reviewed
per-message retry/fork. These routes are independent backend contracts; browser
wiring remains deferred for owner review.
