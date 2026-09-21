# Scheduled jobs

Pi owns schedule definitions and run admission. ToolGate owns effects and approvals.
Each definition targets one exact published tool or automation version and SHA-256
digest; instructions describe its purpose, rather than asking an AI to reconstruct
the workflow at each tick. Agent IDs are selection metadata, not credentials.

Owner-only `/jobs` routes create/list definitions, update with an expected revision,
read run receipts, and admit manual runs using a stable request ID. Disabling a
schedule stops future automatic admission; an explicit manual run remains possible.

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
Browser gateway/frontend integration is deferred
until owner review. Tests use temporary databases and no external effects.


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
route or frontend adapter was wired in this phase.
