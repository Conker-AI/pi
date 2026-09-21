# Durable future-turn queue

Owner-only endpoints manage ordinary conversation queues:

- GET/POST `/sessions/{id}/queue`: inspect/enqueue, with a retained request ID,
  text (1-4000 characters) and at most five extracted-text attachment IDs.
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

This commit provides durable lifecycle controls only. Queue execution/worker,
atomic handoff into submission reservation, restart reconciliation and automatic
draining are still pending. Pause/resume does not yet dispatch. Additional
frontend fields (per-entry model override, reply target and research controls)
need the corresponding execution contracts; they are not silently accepted or
ignored. Final browser wiring is deferred to owner review.
