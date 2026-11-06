# Forgetting a memory

Owner-only. Forgetting removes one memory everywhere it is stored:

- **MemoryGate** (through the dedicated correction capability, never an admin key):
  the memory row, its revision snapshots, conflict records naming it, and the text in
  its audit entries. A content-free audit entry and a deletion receipt (for recovery
  replay) record that it happened. The vector point is removed after commit; the
  receipt reports `removed` or `degraded`.
- **Pi**: every cached per-turn memory package that quoted it is cleared.

The source conversation is kept. Forget the chat separately to remove it too.

## Flow

1. `GET /memory/forget/{memory_id}` returns the exact current text and revision.
2. `POST /memory/forget` with `request_id`, `memory_id`, `expected_revision`.
   It succeeds only if that revision is still current (409 otherwise). The request id
   makes it idempotent: after a lost reply, repeat the same request to check.

Through the browser both are owner-control operations; the write needs the owner
password. Requires `PI_MEMORY_CORRECTION_URL`, `PI_MEMORY_CORRECTION_KEY` and
`PI_MEMORY_CORRECTION_AGENT_ID` (see `memory-proposals.md`); without them the routes
answer 503 `not_configured`.

## Several at once

`POST /memory/forget-many` with `request_id` (16-120 characters) and `items` (1-50, each
`memory_id` and `expected_revision`, no memory twice) forgets them one at a time, in order, each
as its own forget with request id `<request_id>-<index>`. The first refusal stops the batch: if
nothing was forgotten yet it is answered as that refusal, otherwise the reply lists `results` and
`stopped` (`index`, `memoryId`, `code`, `message`). Repeating the same batch replays the items
already forgotten from Pi's receipts and goes on from the one that stopped. Through the browser it
asks for the password once for the whole batch.
