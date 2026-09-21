# Durable future-turn queue

Owner-only endpoints manage ordinary conversation queues:

- GET/POST `/sessions/{id}/queue`: inspect/enqueue, with a retained request ID,
  text (1-4000 characters), optional model_id and at most five extracted-text
  attachment IDs.
- PATCH `/sessions/{id}/queue/{entry}`: edit text using expected_revision.
- POST `/sessions/{id}/queue/{entry}/review`: explicitly recapture current choices.
- POST `/sessions/{id}/queue/{entry}/remove`: remove an unclaimed entry.
- POST `/sessions/{id}/queue/pause` or `/resume`: change pause state using the
  current queue revision. Resume validates selections; it never updates them.

There are at most five waiting/claimed entries. Enqueue IDs are permanent replay
identities; repeated requests cannot recreate removed entries or replace payloads.
Edits and review advance entry revisions. The execution configuration, agent,
privacy, model catalogue revision and context policy are captured. A mismatch
pauses the queue and requires explicit review. File availability and privacy are
rechecked; deleting an unused upload can pause its queued entry, retaining text.
No credentials are supplied through these endpoints. Session forgetting scrubs
queued text, configuration snapshots and original payload hashes.

Queue execution uses `POST /sessions/{id}/queue/run-next` (owner-only), or the
backend worker enabled with `PI_QUEUE_ENABLED=1`. The worker is disabled by
default until explicitly configured. A tick processes at most one entry per
selected conversation; subsequent ticks drain successful turns in FIFO order.
Admission into the ordinary submission ledger atomically checks queue position,
pause state, revision, payload, file availability and frozen settings. The durable
submission ID derives from the queue identity and revision. Provider calls happen
outside transactions and only the caller that reserves the submission executes.

Busy ordinary turns are waited for. Approval/budget/unknown-effect holds pause
following messages. Failed/stopped entries remain visible and must be removed
before resuming; intentional retries use a new enqueue identity. Startup marks
interrupted submissions before worker start; reconciliation inspects receipts and
never automatically reruns submitted work. Completion surviving a lost response
retires its queue entry without another provider call. Pause affects future
admission; use Stop to cancel the currently executing turn.

The optional model_id selects an enabled eligible answer model from the captured
catalogue. It is bound into the immutable submission snapshot and never silently
falls back. Explicit queue review may select a different model (or null for the
normal configured route). Invalid models roll back before submission reservation.

Additional frontend fields (reply target and research controls) need the
corresponding execution contracts; they are not silently
accepted or ignored. Final browser wiring remains deferred to owner review.
