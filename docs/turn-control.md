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

`POST /turn-submissions/{request_id}/cancel` accepts the retained submission ID
before or after turn binding. During preparation it preserves pending input and
drafts, releases upload reservations, interrupts context selection and prevents
a late summary from creating a fork. The terminal preparation receipt uses
`failure_code=owner_cancelled` and presents `status=cancelled`. The same request
ID only replays the receipt; an intentional resend requires a new ID. A helper
already in flight may finish remotely, but cannot dispatch the answer.

`POST /turns/{turn_id}/reply-only` with an owner credential and a fresh
`request_id` permits narration of an acted_no_reply turn whose action outcomes
are all resolved. Claiming and recording the request are atomic. Reusing the ID
only inspects status; a failed attempt needs an intentional new ID. The original
action and cancellation evidence remain. The model receives the saved history
and a reply-only instruction without available tools; recovery never dispatches
action requests. A new Stop invalidates the old consent at provider checkpoints
and final persistence. Normal resume does not inherit this permission. Unknown
effects must first be reconciled. Existing call privacy guards still apply.

Remaining P9 work: queued future turns, steering, and reviewed
per-message retry/fork. These routes are independent backend contracts; browser
wiring remains deferred for owner review.

## Per-request model selection

Ordinary POST /sessions/{id}/turns accepts optional model_id. It must identify an
enabled model/provider eligible for the answer role in the configured catalogue.
The selection is part of the request identity and immutable submission/turn
snapshot, taking precedence over the agent's default answer model. Invalid choices
roll back admission; failures do not fall back to a different model. Helpers retain
their independently assigned roles. Call/team model controls remain separate.
This selection also applies to reply-only recovery of that turn. Selecting another
model for an existing response requires the still-pending per-message retry flow.

## Reply targets

Ordinary turn and queue creation accept optional reply_to, an existing user or
assistant message in the conversation (including explicitly inherited context).
Foreign, forgotten and excluded messages are rejected. The ID is part of the
request identity and frozen snapshot; input-message reads expose reply_to. Model
context marks the selected message as the reply target without elevating its
contents to instructions or grants. The target must survive the explicit context
selection; a retrieval helper omitting it stops the request for context review.
Automatic summarization cannot silently discard it. Queue review can explicitly
change or clear the target; Resume alone cannot do so.
