# Steering a running conversation

Queue saves a future message. Stop prevents future execution checkpoints. Steering
changes the instructions of the current ordinary conversation turn at its next
planning boundary, preserving the same turn, task association and completed effects.

Owner POST `/turns/{id}/steer` accepts `request_id` and `text` (up to 4000 characters).
GET `/turns/{id}/steering` returns pending/applied/not_applied receipts and metadata
for model answers discarded because a newer steering instruction arrived.

The request atomically appends an immutable user message and a retained identity.
Duplicates inspect the same receipt. There are at most ten accepted steering inputs
per turn. Running call/team sessions and narration retries keep their own controls.
Only an active ordinary turn accepts new steering. Pending inputs interrupted by
process shutdown become not_applied; inspection never silently executes them again.

Before any new tool action is admitted, and before the final answer is committed,
Pi checks for pending steering. Stale provider output is discarded, context is
rebuilt, and the same turn continues with the original tool-step ceiling. Original
privacy, agent and model configuration remain frozen; steering grants no authority.
Completed tool receipts and acted state survive later provider failure.

An already admitted tool action cannot be retracted by steering. Such a request
returns action_in_flight without appending an instruction; wait for the result or
use Stop to prevent subsequent work. This includes actions parked for approval.
This limitation is explicit rather than pretending an in-flight effect was changed.

Applied means included in a prepared model input, not proof that the turn succeeded.
The separate turn status remains authoritative. Context preparation cannot silently
exclude accepted steering, and reading a finished turn cannot change its receipts.
Discarded-answer provider usage is retained. A successful turn sums reported usage
with discarded answers; any unknown component keeps the corresponding total unknown.
No raw discarded answer or extra copied source transcript is stored in that ledger.

The frontend currently lacks a steering control. It remains a tracked frontend
contract gap; these owner APIs are backend behavior, not final browser wiring.

Verification: 921 passed, 8 skipped in the complete isolated Pi suite; 10 focused
steering checks also passed after the final receipt projection adjustment. Existing
Starlette/httpx deprecation warning remains. Focused Ruff and diff checks passed.
