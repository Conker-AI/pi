# Durable context selection

Messages marked `retrieve` now use the configured `context-selection` model role
before a new turn is bound. This is an actual dispatcher call using server-owned
adapters, not a fixture choice or MemoryGate search. Tests use stub adapters only.

Submission reservation freezes the current context policy and eligible candidate
IDs alongside the existing frozen agent, privacy and model-role configuration.
The helper receives the current submitted user query plus only `retrieve` message
IDs, roles and text. Excluded messages, exact pins, other history and expected
answers are not sent to this helper. Candidate text is read from the authoritative
transcript rather than copied into another durable text store.

The response must be exactly JSON `{"messageIds":["..."]}` with distinct IDs from
the frozen candidate set. Empty selection is valid. Duplicate JSON keys, additional
fields, unknown/foreign IDs and malformed JSON fail closed before answer dispatch.
The answer retains original chronological order and exact content; the helper
cannot rewrite messages or drop `keep-exact` pins. It can only include or omit the
explicit retrieval candidates.

No-harness blocks the call. Messages originally excluded from harness use cannot
be sent to it after future privacy settings change. No-memory is independent: it
continues to govern MemoryGate access, while local context selection can run when
harness use is allowed. Missing source/privacy metadata, forgotten candidates and
missing/disabled role configuration fail closed. Linked project/file content is
not silently added to helper inputs, and attachment extraction is unchanged.

Selection is bounded to 200 candidates and 64000 serialized input characters.
Exceeding those bounds fails explicitly; candidates are never silently truncated.
Fixed context, including exact pins and the pending user query, is checked against
the existing estimated context budget before selection. The complete answer context
is checked again afterwards, including the usual instruction/tool/attachment layers.
An over-budget selected result fails instead of dropping pins. These are the existing
token estimates, not a provider tokenizer or a guarantee about its true window.

## Receipts and recovery

`submission_context` stores frozen policy, candidate IDs, state, selected IDs, and
successful dispatcher metadata (configuration revision, requested/actual models,
reported usage and latency). It stores neither helper output text nor another copy
of candidate transcript text. Successful selections bind to the exact turn.
`_history` reads that result on repeated calls, tool continuations and resumes; it
never invokes the helper. Later context edits do not rewrite the turn's policy.

Only the new submission owner can claim a reserved selection. A failed, running or
interrupted receipt cannot automatically execute again. Identical submission retries
return their existing receipt. At startup `context_retrieval.recover_interrupted`
marks unfinished selections without redispatch; it must run before serving requests.
A late response cannot overwrite an interrupted result. Explicitly submitting a new
request may run a new helper; recovery does not imply remote cancellation.

Admin GET `/context/{session_id}/selections/{request_id}` inspects the receipt without
starting work. Existing GET `/context/{session_id}/turns/{turn_id}` returns the frozen
submission policy for newly bound turns. Reviewed forks remain explicit: unresolved
`retrieve` policies must be reviewed before providing an owner-approved summary.

Initialize `context_retrieval.SCHEMA` after the submission/context tables. Offline
forgetting calls its redaction through `context_controls.redact`, removing copied
session instructions and selection details while retaining the receipt identity.
Preparation failure and attachment reservation release now share one transaction;
startup cleanup also removes stale reservations on already-failed preparations.

Validation (temporary databases, no network providers):

```sh
python -m pytest tests/test_context_retrieval.py tests/test_context_controls.py tests/test_submissions.py tests/test_attachment_turns.py tests/test_loop.py tests/test_forgetting.py -q
```
