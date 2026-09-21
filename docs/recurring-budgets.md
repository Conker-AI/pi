# Scheduled recurring budgets

A job may specify `requireBudget: true` and `budgetAllowanceId` referencing an
owner-created ToolGate allowance. Pi uses its existing agent execution key to
allocate one budget for each durable scheduled run, then validates and binds it
using the existing budget API. The saved definition is the target of allocation.

The worker retries admission after a lost response using the same run ID. Failed,
expired, mismatched or exhausted admission leaves the run awaiting_budget without
dispatching effects. Held runs are processed in rotating batches separately from
ready runs. Authenticated POST `/jobs/runs/{id}/provision-budget` also attempts
admission (under the configured jobs router prefix).

Changing a schedule never expands ToolGate authority. The owner must create a new
matching allowance when changing target version, digest, arguments or actor.
Cancellation racing allocation may consume an unused ceiling but cannot dispatch
the cancelled run; ceilings are never automatically refunded. Unknown dispatched
effects still require reconciliation, not replay. Browser wiring is deferred.
