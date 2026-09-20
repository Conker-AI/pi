# Durable Tasks, Runs and Events

Pi stores the owner's task ledger in the same SQLite database as conversations.
Tasks are outcome/criteria/status metadata. Creating, revising, linking, cancelling
or archiving a task does not call a provider, dispatch a tool or stop a turn.
`status_source: owner` distinguishes an owner's completion review from execution
evidence. Only Companion assignment is available; there is no durable specialist
registry in this increment.

Runs are existing Pi turns, retaining every recorded state, including approval or
budget waits, action-in-progress, uncertain outcome and acted-without-reply. They
are not copies of the execution history. `outputs` is currently empty; new turns
expose exact `message_refs`, while historical messages are never associated by
guessing. Message references do not claim that an artifact was generated.
ToolGate remains authoritative for effects,
credentials, approvals, action identities and execution receipts.

## API

All routes require Pi's recovery credential or its scoped gateway credential.
Browsers use the HTTPS cookie/CSRF gateway at `/api/pi/...`; no service credential
belongs in a frontend. Existing fresh-verification/deployment acceptance work
remains separate from these metadata operations.

| Method/path | Request or result |
| --- | --- |
| `GET /tasks` | `{results: Task[], next_cursor}`; `limit` 1–200 (default 50), optional `cursor`, `session_id` |
| `GET /tasks/{id}` | One Task |
| `GET /tasks/requests/{request_id}` | Current Task for a previously recorded creation request, or 404 |
| `POST /tasks` | `request_id`, `session_id`, `outcome`, `criteria: string[]`, optional `parent_task_id`, `run_ids` |
| `POST /tasks/{id}/update` | `expected_revision`, `outcome`, `criteria`, `parent_task_id`, `run_ids`; send the whole desired configuration |
| `POST /tasks/{id}/transition` | `expected_revision`, `status`, `note`, optional `completed_criterion_ids` |
| `POST /tasks/{id}/archive` | `expected_revision`, `archived: boolean` |
| `GET /runs` | `{results: Run[], next_cursor}`; task list query fields plus optional `task_id` |
| `GET /runs/{id}` | One actual turn projection |
| `GET /events` | `{results: Event[], next_cursor}`; run list query fields plus optional `run_id` |

Task writes return the current Task directly. Task/run cursors are record IDs;
event cursors are decimal sequence strings. Dates are Unix epoch seconds.
Task errors use `{detail: {code, message, current_revision?}}`, with HTTP 409 for
conflicts, 404 for missing records and 422 for invalid request shapes.

Task fields: `id`, `session_id`, `agent_id: companion`, `parent_task_id`, `outcome`,
`criteria: [{id,text}]`, `run_ids`, `status`, `status_source: owner`,
`provenance: recorded`, `revision`, `created_at`, `updated_at`, `archived_at`,
`status_note`, `completed_criterion_ids`, `content_status`, `changes`,
`changes_truncated`. Changes contain the latest 100 direct, content-free task
events in ascending order; fetch `/events?task_id=...` for paginated inspection.
Only the current owner note is retained as text; events do not copy historical
outcomes, criteria or review prose.

Run fields: `id`, `session_id`, `status`, `acted`, `provider`, `model`, `started_at`,
`ended_at`, `task_ids`, `action: {id,state,job_id} | null`, `outputs: []`,
`source: {kind: conversation,session_id}`, `provenance: recorded`, `content_status`,
`message_refs: [{message_id,purpose,action_id,seq}]`. See [submission recovery](turn-submissions.md).

Event fields: `sequence`, `id`, `kind`, `session_id`, nullable `task_id`, `run_id`,
`action_id`, `from_status`, `to_status`, `revision`, plus `occurred_at` and
`content_status`. Events record actual observed state changes, not hidden model
reasoning. A task event filter includes its direct events and the events of its
**currently explicitly linked** runs. This is an inspection view, not a claim of
historical ownership. Unlinking keeps link/unlink events; full run history remains
available by `run_id`.

## Lifecycle, transactions and recovery

- Outcome: 1–1,000 characters. Criteria: 1–20 distinct items, each 1–500 characters.
  Status review note: 1–2,000 characters. A task links at most 100 distinct runs.
- The session is fixed. Parents and linked runs must belong to that exact session;
  automatic conversation forks do not silently move task ownership. Parent
  ancestry is bounded to 100 levels and cannot form a cycle.
- New work requires an open source conversation. A closed source still permits
  inspecting/editing metadata and terminal owner decisions; it cannot be resumed
  into an active task. Forgotten sources permit no task mutation.
- `planned → in_progress | blocked | cancelled`;
  `in_progress → blocked | completed | cancelled`;
  `blocked → planned | in_progress | cancelled`;
  `completed | cancelled → planned`.
- Completion requires every current criterion ID. Terminal tasks must be reopened
  before editing. Archive requires a terminal task. Active children block parent
  completion/cancellation/archive, and a child cannot resume under an inactive
  parent. Cancelling a task never claims to cancel an in-flight effect.
- `BEGIN IMMEDIATE` covers reference checks, revision comparison, metadata writes,
  links and events. A failed write rolls back its event too. Revision conflicts
  require rereading and explicit owner action, not an automatic overwrite.
- Creation requires a caller-retained 16–128-character alphanumeric/underscore/
  hyphen request identity. Identical normalized payload replay returns the current
  task without creating another task/event. Different payload reuse fails. Read
  by request ID after a lost acknowledgement before deciding whether to retry.
- Additive schema setup upgrades existing databases without generating fake old
  events. Database triggers record subsequent turn/action transitions, including
  restart interruption and approval resume. Event update/delete/replace is refused
  by SQLite. Existing Pi startup recovery and ToolGate reconciliation remain in use.

## Privacy and durability

Task text is stored only in Pi; task writes do not enter MemoryGate's message
outbox. The offline owner forgetting transaction scrubs task outcome, criteria and
notes across the same session/descendant scope, and removes creation-payload
comparison hashes. Forgotten request IDs stay reserved; their read endpoint returns
a tombstone. Task IDs, revision, status, run links and content-free events remain.
Views additionally mask unavailable source text and report `content_status:
forgotten`. Run provider/model and action/job identities remain metadata, matching
Pi's existing forgetting contract; no tool arguments or turn detail are projected.

The ledger lives in `pi.db` and survives closing/reopening Pi. A consistent backup
of that existing database includes it; no separate volume or service is added.
Backup/restore deployment exercises remain P15 work. Task metadata identities are
separate from the optional retained identities in [turn submission](turn-submissions.md).
Clients omitting a turn request identity still cannot safely repeat a lost POST.
