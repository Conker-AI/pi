# Scheduled jobs

Pi owns schedule definitions and run admission. ToolGate owns effects and approvals.
Each definition targets one exact published tool or automation version and SHA-256
digest; instructions describe its purpose, rather than asking an AI to reconstruct
the workflow at each tick. Agent IDs are selection metadata, not credentials.

Recovery-admin `/jobs` routes create definitions and replace their complete pinned
target with an expected revision. The narrower owner-browser contract can list and
inspect existing schedules, pause or enable one with an expected revision, inspect
redacted run history, and admit a manual run using a stable request ID. Disabling a
schedule stops future automatic admission; an explicit manual run remains possible.

The browser routes are exact and schema version `1`:

- `GET /jobs?limit=50&cursor={job_id}` and `GET /jobs/{id}` return bounded safe
  projections. Free-form instructions, target arguments and allowance identifiers
  are replaced by configured booleans.
- `POST /jobs/{id}/state` accepts only `expected_revision` and `enabled`. It changes
  durable schedule state and reports `execution: not-triggered`; it does not dispatch
  as part of the request. Enabling permits the separate worker to admit future due
  occurrences.
- `GET /jobs/{id}/runs?limit=50&cursor={run_id}` returns at most 100 redacted rows.
  Raw definitions, arguments, request IDs, approval IDs, budget IDs and receipts are
  never returned.
- `POST /jobs/{id}/run` explicitly admits one manual occurrence. Its 16-128 character
  request ID is the durable idempotency key. The response distinguishes admission,
  waiting, dispatched, resolved and uncertain state; admission never claims the
  external operation ran.
- `POST /jobs/runs/{id}/budget`, `/provision-budget`, `/cancel`, `/resume`, and
  `/reconcile` are explicit owner actions against one canonical saved run. Their
  responses expose only bounded status evidence and no credential or authority.

Every safe job/run DTO declares `authority: none` and whether content was included.
The conversation runtime credential cannot use these routes. Creation and full
definition replacement are deliberately absent from the owner browser allowlist:
Pi cannot currently inspect a ToolGate publication's argument schema, so accepting
browser-supplied target arguments would permit arbitrary values, including paths or
credential-shaped content. Recovery admins retain the pre-existing authoring API
until an authoritative publication catalogue and schema resolver exist.

Daily and weekly schedules use IANA time zones. Missing daylight-saving slots are
skipped; repeated slots run once at the first occurrence. Intervals use elapsed
UTC hours anchored at creation. Missed slots coalesce to one admission, and a run
that has not resolved blocks overlapping runs. Schedule edits do not mutate runs
already admitted.

Runs progress from `ready` to `dispatching` through a transactional single-use
claim, then to `completed`, `failed`, `awaiting_approval`, or `outcome_unknown`.
Dispatch reads the stored target, never a caller-supplied target. Replaying a
dispatch does not call its adapter again. A crash after dispatch admission leaves
the run held for reconciliation; an exception never means an effect did not occur.

## Remaining integration within the backend

The schedule store, owner API and opt-in timer worker are implemented.
`PI_SCHEDULER_ENABLED=true` starts the worker only when a scoped ToolGate credential
is configured. It processes automatic and manual ready admissions, including those
retained across restart. Multiple workers share transactional dispatch claims.
Shutdown waits for the in-flight bounded adapter before closing the store.
This setting has not been enabled on the user's services.
`PublishedJobs` invokes pinned publications with server-provisioned per-agent scoped
ToolGate clients. Unknown agents fail without borrowing the companion credential.
It verifies action identity, version and digest before accepting completion. Its
reconciliation path only reads ToolGate receipts; missing receipts never allow a
retry. `jobs.reconcile` saves resolved outcomes without overwriting final states.
Execution and reconciliation disable inherited proxy settings and redirects, request
identity encoding, and cap receipt bodies at 256 KiB with an elapsed-time check
using the configured client timeout. Each blocking read also has the HTTP timeout;
this is not a preemptive wall-clock cancellation of an in-flight socket read.
Encoded, oversized, late, malformed, and transport-failed responses remain unknown,
not permission to retry. The adapter closes streams on success and failure.
Approval resume uses the saved request ID, action ID, agent and published inputs.
Its transactional claim prevents concurrent resumes; ToolGate decides whether the
approval is valid. A timeout is held for read-only reconciliation. Owner-only
`/jobs/runs/{id}/resume` and `/reconcile` routes expose these operations. The existing
companion execution credential provisions the companion adapter; other agent IDs
require separate server provisioning and cannot borrow it.

Definitions can set `requireBudget: true`. Each admitted occurrence then waits in
`awaiting_budget` and blocks overlap without making any effect request. The owner
creates a ToolGate spending job bound to the provisioned execution actor and the
saved Pi run ID, then calls `POST /jobs/runs/{id}/budget` with `{budget_id}`.
Pi verifies the budget through the agent-scoped read endpoint, saves the binding
once, and releases that run to `ready`. Listing exposes `spending_budget_id`.
Approval resume retains the same binding; a different or reused budget is rejected.
ToolGate still validates actor/root identity and reserves costs at actual dispatch.
This binding does not promise available credit or bypass current policy/price checks.

The worker never creates budgets or receives an administrative credential. Every
later occurrence needs its own owner-authorized budget; automatically provisioning
recurring grants and proactive owner-budget reservations remains outstanding. Explicit
owner schedules are distinct from unsolicited proactive suggestions; notification
quiet hours should not silently cancel a deliberately scheduled operation.
Manual run admission records `ready`; execution requires the worker to be enabled.
Reading a schedule or run never admits or dispatches work. Tests use temporary
databases and injected adapters; they make no external effects.


## Withdrawing waiting runs

Owner-only `POST /jobs/runs/{id}/cancel` withdraws runs in `ready`,
`awaiting_budget`, or `awaiting_approval`. It requires no execution adapter and
shares the dispatch transaction lock: once dispatch is claimed, cancellation
fails and the owner must reconcile the outcome. Unknown effects cannot be made
safe by relabeling them cancelled. Repeated cancellation is idempotent, survives
restart, preserves receipts/budget bindings, and releases schedule overlap.
Future scheduled occurrences are unaffected; disable the definition to stop them.
Continuity treats cancellation as a resolved status rather than an attention alert.

This operation withdraws Pi's dispatch only. It does not revoke a ToolGate approval
or return a spending grant to a reusable pool. Saved approvals remain evidence;
ToolGate authority outside this scheduler is administered separately. No browser
response exposes the saved approval, receipt or budget identifier.


## AI jobs (2026-10-25)

An AI job is a cron job whose target is `{"kind": "agent"}`: at each slot its agent carries out
the job's instructions in one ordinary chat turn, in the job's own chat, after a fixed preamble.
Nothing about what a job does is built in; a weekly life review, a deadline watch or a follow-up
with friends are all instructions. ToolGate decides every tool call exactly as in chat: tools
that need no confirmation (get tools) run on their own, and a tool that needs the owner's OK
(set tools) parks the turn as an Inbox approval. Pi grants nothing of its own
(`pi/schedule/agent_jobs.py`).

- Timing is any cron timing: every N hours, daily at a time, or weekly on a day. Repeating
  (interval) jobs skip slots inside the owner's quiet hours; a job set for a time of day runs then.
- The run ID is the turn's request ID, so a repeated dispatch replays the saved turn instead of
  running it twice. Reconcile reads the saved submission; a missing one is never permission to run.
- A reply of exactly `NOTHING` stays quiet, which is how a condition in the instructions ("only
  if...") is expressed. Any other reply adds one Inbox note (`kind: "agent-job"`): its first line,
  up to 160 characters, and the next 400 characters, linking to the chat.
- AI jobs carry no job budget (`requireBudget` is refused); each tool keeps its own limits.
- Their chats are left out of the Ideas pass, which would otherwise read the scheduled prompt as
  something the owner keeps asking. Forgetting a job's chat deletes its notes; the next run starts
  a new chat.
- Owner routes: `POST /jobs/agent` creates one (name, instructions, timing, time zone; never a raw
  tool target, and the gateway asks for the password), `GET /jobs/agent/notes` lists open notes,
  `POST /jobs/agent/notes/{id}/dismiss` hides one (signed-in session only).

### Starting from a tool (2026-10-25)

An AI job with `watch` (`toolId`, `args`, `everyMinutes` 5-1440) starts when that get tool has
something new instead of on its timing (`pi/schedule/agent_triggers.py`). At each look Pi calls
the tool through the agent's scoped ToolGate client, exactly as a chat would, and compares a
SHA-256 fingerprint of the answer with the last one; Pi stores only the fingerprint and when it
looked, never the answer. The first look is a baseline. A change admits one run (request
`watch_<fingerprint>_<last look>`, so the same change seen twice replays); the turn is told which
tool has something new and reads it with its own tools. A change while a run is still going keeps
the old fingerprint, so the next look starts the job. A tool that asks for the owner's OK each
time stops the watch once (`problem: "needs_approval"`) instead of asking every few minutes;
other failures are retried at the next look. Watches pause in quiet hours. Changing the watch
starts over. The browser view shows the watch's tool, interval, last look and problem.

- Next: a webhook and Conker events (a new Inbox item, another job finishing) start the same turn.
