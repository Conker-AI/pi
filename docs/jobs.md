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

The schedule store and owner API are implemented. No timer worker is started yet.
`PublishedJobs` invokes pinned publications with server-provisioned per-agent scoped
ToolGate clients. Unknown agents fail without borrowing the companion credential.
It verifies action identity, version and digest before accepting completion. Its
reconciliation path only reads ToolGate receipts; missing receipts never allow a
retry. `jobs.reconcile` saves resolved outcomes without overwriting final states.
Approval resume uses the saved request ID, action ID, agent and published inputs.
Its transactional claim prevents concurrent resumes; ToolGate decides whether the
approval is valid. A timeout is held for read-only reconciliation. Owner-only
`/jobs/runs/{id}/resume` and `/reconcile` routes expose these operations. The existing
companion execution credential provisions the companion adapter; other agent IDs
require separate server provisioning and cannot borrow it.

The timer lifecycle, grants and owner budget admission must be connected
before enabling automatic work. Manual run admission currently records `ready`
and does not promise execution. Browser gateway/frontend integration is deferred
until owner review. Tests use temporary databases and no external effects.
