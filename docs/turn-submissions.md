# Durable turn submission and exact message provenance

Pi can reconcile a turn request without repeating it. This increment does not
claim exactly-once external effects: ToolGate's action identities, approval
binding and receipt reconciliation still govern execution.

## Caller contract

`POST /sessions/{session_id}/turns` retains its existing text and routing-hint
fields and accepts these optional fields:

```json
{
  "text": "Prepare the requested answer",
  "needs_tools": false,
  "is_analysis": false,
  "owner_requested_strong": false,
  "request_id": "caller_retained_unique_identity",
  "task_id": null,
  "task_expected_revision": null
}
```

Generate and retain a request identity **before** sending. It is 16–128 characters
from `A-Z`, `a-z`, digits, `_` and `-`. Task identity/revision must be supplied
together, and require an explicit request identity. A task must be active,
unarchived, at the expected revision and in the exact requested conversation.
Attaching its run increments the task revision but does not change owner-reported
status or mark completion criteria reviewed.

The first successful response retains the existing turn result and adds
`submission` plus `replayed: false`. Repeating the same normalized request identity
and payload returns the **current** receipt/result with `replayed: true`; it never
calls a provider, prepares another fork or invokes an action. Reusing the identity
with different text, session, routing context or task binding fails with HTTP 409.
Check the receipt after a lost acknowledgement rather than automatically repeating
a POST. Initial provider failures retain the existing 503/turn-ID response and
include the caller's request ID; acted-without-reply responses include the receipt.

Clients omitting request IDs remain supported, but each POST creates a different
internally identified submission. Those clients gain exact message associations,
not retry safety. Existing resume and explicit manual-fork endpoints have **not**
gained request idempotency. Resume retains its compare-and-swap claim and original
action reconciliation; callers must inspect a lost resume response before acting.

## Read and reload recovery

- `GET /turn-submissions/{request_id}` returns the current receipt.
- `GET /sessions/{id}` includes up to 100 content-free `pending_submissions`
  references, with `pending_submissions_truncated` if more exist.
- `GET /sessions/{id}/submissions?limit=50&cursor=...` pages those unresolved
  references, including interrupted/failed preparation with retained input.
  `limit` is 1–200; the cursor is the previous page's request identity.

Receipt fields:

```text
request_id, requested_session_id, effective_session_id?, turn_id?, task_id?,
input_message_id?, final_message_id?, state, status, acted,
message_refs: [{message_id, purpose, action_id?, seq}],
pending_text?, failure_code?, created_at, updated_at, content_status
```

Nullable values are JSON null. Times are epoch seconds; `updated_at` records the
receipt stage change, while the associated run holds execution timing. `state`
is `preparing`, `bound`, `preparation_failed`, `preparation_interrupted` or
`forgotten`. `status` is the actual turn status once bound, otherwise the receipt
state. `pending_text` retains input only before it becomes a canonical message.
`failure_code` contains a static preparation code, never raw provider text.

Content-free references contain only `request_id`, requested/effective session
IDs, nullable turn ID, state/status, times and content status. They never contain
pending input, hashes or message bodies. Preparation failures remain discoverable;
there is no hidden automatic recovery or dismissal executor.

Read routes use the same authenticated gateway boundary at `/api/pi/...`; no
service keys are sent to browsers. Routing hint `needs_tools: false` is **not** an
effect-disable control. Fresh verification and ToolGate policy remain separate
requirements before exposing privileged operations.

## Transaction and crash boundaries

1. Reserve the request before any provider call, including the automatic fork
   summarizer. Store pending input and the source history sequence durably.
2. Only the reservation's creating caller prepares the request. A competing
   request is refused while the session has preparing/running or parked unresolved
   work. Replays of the original identity remain readable.
3. Run any summary outside SQLite transactions. Recheck source history/session
   state and task revision before binding.
4. Commit parent closure/child creation (when needed), input message, turn, exact
   input association, optional task link and bound receipt atomically. Failed
   binding cannot leave an orphan child, user message or memory outbox delivery.
5. Dispatch through the existing loop. ToolGate action identity is still committed
   before invocation. Tool observation, exact association, action state and acted
   flag share a commit. Final answer, final association and completed turn share
   another commit, so a visible final answer cannot be left behind by a failed
   completion write.

Startup marks unfinished preparation `preparation_interrupted`, retaining input.
It never reruns the summarizer or starts a turn automatically. Existing turn
recovery distinguishes interrupted inference, uncertain dispatch and an action
whose reply is missing. A replay only inspects those states. Unknown action
outcomes reconcile the original action ID; acted-without-reply resumes only the
reply. Preparation retry/continuation UI and idempotent resume requests are later
work, not inferred from this receipt.

Automatic forks report both original and effective session IDs. A task-bound
request requiring a fork currently fails **before** invoking a model, preserving
its pending input. Task ownership is fixed to its original session until an
explicit cross-fork task-lineage design is implemented.

## Message associations and privacy

`turn_messages` records exact input, intermediate assistant, tool-result and final
message identities, with tool action identity where known. It is append-only at
the SQLite level, including replace protection for unique input/final/action
associations. New loop messages populate it; historical messages remain
unassociated. `/runs` and `/runs/{id}` expose these `message_refs` without inferring
artifacts or hidden model reasoning. Transcripts remain append-only.

Submission identities/origins cannot be replaced or deleted through normal SQL
writes. The offline forgetting transaction clears pending input and comparison
hashes for both requested/effective session scope, retaining content-free receipt
and message IDs. Forgetting previews bind pending submission IDs as well as
message/turn IDs, so input accepted after a preview invalidates that confirmation.
Physical database/sidecar checks cover the newly retained text and hashes.

State lives in the existing `pi.db` and survives restart. No new worker, listener,
provider connection or external effect service is introduced.
