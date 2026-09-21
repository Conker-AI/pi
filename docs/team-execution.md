# Owner-directed team execution

Pi now executes saved team definitions through its ordinary Loop, durable submissions,
task/run associations and ToolGate boundary. This is an owner-directed coordinator:
it does not infer handoff conditions, spawn background workers or promote results.
Gateway/frontend wiring and deployment remain separate.

All `/team-runs` routes require the owner Pi key. `POST /from-team/{teamId}` accepts
`request_id`, `expected_revision`, `source_session_id`, `text`,
`budgetMode: "observed-stop"`, and `acknowledgeCurrentCallMayOvershoot: true`.
It freezes the team revision, agent versions, configured model roles and current
privacy. It records the owner task in a child conversation, not a second text log.
Starting a run does not call a provider.

`POST /{runId}/steps` actually executes one role. Its body is `request_id`,
`expected_revision`, `roleId`, and, after the first step, `handoffId` plus
`conditionReviewed: true`. Each step gets a dedicated task/conversation and stable
submission identity. A new step can follow only a completed result over a declared
edge. The condition is displayed in the frozen definition for owner review; Pi
does not pretend to evaluate its natural language. Task completion criteria still
require owner review.

`GET /{runId}` returns the frozen definitions, step/task/session references,
requested and actual models, reported usage and latency, and explicit limit semantics.
`POST /{runId}/reconcile` inspects the existing submission after a separately authorized
approval/resume; it never dispatches anything. `POST /{runId}/finish` accepts
`expected_revision` and `state: "completed" | "cancelled"`. Completion is an owner
decision. Pending and interrupted work cannot be described as completed.

## Scope and limits

- Turn allocations, directed edge counts and total handoffs are enforced before
  dispatch. Failed/uncertain steps consume their allocation. Reusing a request ID
  inspects the receipt; it never repeats provider calls or tool effects. Conflicting
  request reuse and stale revisions fail.
- Token and cost fields are **observed stops, not hard spending ceilings**. Existing
  provider adapters do not guarantee a maximum output size or cost. Every actual
  provider call, including intermediate tool replies, is durably recorded; the current
  call can overshoot. Its response is withheld from further tool/answer processing
  when a threshold is reached. Unknown, malformed or missing usage blocks further
  calls, including configured fallback attempts. Zero cost allocation blocks all calls
  because the adapter cannot guarantee zero cost in advance. No acknowledgement or
  budget number grants paid-provider or tool authority.
- Tool charges are governed separately by ToolGate and are not included in model
  spending totals. Each role receives only its tool subset intersected with current
  ToolGate availability, even when its authored agent is the Companion. The next
  role never inherits the previous role's tools or approvals.
- Team memory currently requires `none`; specialist memory namespace authorization
  is not implemented. Role copies are excluded from Companion memory ingestion.
  Source conversation privacy is retained/narrowed, never widened.
- Selected context currently means available message IDs from the explicitly named
  source conversation. Private/forgotten/foreign sources fail closed. Project files,
  arbitrary artifacts and unverified IDs are not silently substituted. `task_only`
  receives only the owner task and explicit incoming handoff. A handoff transfers only
  the final answer, with supplied citation metadata only for `result_and_citations`;
  it never transfers the prior role's transcript or tool observations.
- Each step is a fresh session with a fixed context policy. No automatic retrieval,
  summarization or fork runs outside metering. Inputs over the 16000-character turn
  boundary fail explicitly rather than truncating selected evidence.

Provider calls are reserved before invoking adapters. Startup changes active runs
and calls to interrupted/unknown; no worker retries them. A late result cannot replace
that receipt. A held approval remains subject to the existing exact ToolGate approval
flow and every subsequent model call still passes through team metering.

Source IDs and prior final-answer IDs are stored as references. Text copies exist
only in normal session transcripts. Sessions form a descendant chain rooted at the
source, so offline forgetting includes owner task and all derived role turns. Forgotten
runs cannot dispatch or expose their views. Authored team/agent configuration history
remains configuration history, not a transcript mirror.

Validation uses real Loop instances and injected recorder providers only:

```sh
python -m pytest tests/test_team_execution.py tests/test_collaboration.py tests/test_loop.py -q
python -m ruff check pi/team_execution.py pi/team_execution_api.py tests/test_team_execution.py
```
