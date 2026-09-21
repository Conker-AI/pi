# Research request contract

Ordinary turn requests and queued messages accept `research_mode`: `off` (default),
`web`, or `deep`. This is requested behavior, not a permission grant and not proof
that a search happened. Off retains ordinary tool availability.

The choice participates in immutable submission/queue identity, survives queued
text edits and review, and is captured as `researchMode` in execution settings.
Omitting the field remains compatible with existing off-mode request identities.
Submission receipts report the requested mode; forgotten receipts hide it.
Call and team-role submissions cannot use this ordinary-conversation selection.

Draft saves accept the same field, sharing the text revision. Submission must
match both saved text and mode. Consuming that revision clears both, without
clearing a newer draft. Existing text-only draft storage migrates additively.

## Web execution

Web requires the selected agent and ToolGate key to expose `research.web`, with
at least one allowed tool step. It offers only that capability for this turn.
The selected answer model produces a validated bounded query (3-240 characters,
at most eight results, recency 1-3650 days), then synthesizes one recorded search.
No full-page reading is implied. Invalid/missing queries fail before dispatch;
additional tool requests cannot run. Off preserves ordinary tool availability.

Searches use existing action identities, approvals, budget holds, cancellation,
unknown-outcome reconciliation and reply-only recovery. After a lost receipt,
resume checks the same action rather than repeating search. A stopped in-flight
search may finish and retain its result; no synthesis runs after Stop, and its
status honestly remains acted_no_reply rather than pretending nothing happened.

Authenticated GET `/turns/{turn_id}/research` projects status, action arguments,
source message references and original tool observations from the existing ledger.
These are fetched evidence, not a claim that every source was cited or verified.
Forgotten turns cannot expose this receipt. Answer retries remain narration-only.

Deep research now saves a validated public plan as a turn-associated intermediate
message. It adaptively queries research.web from prior results, with at most four
searches (or the configured lower tool-step limit), then synthesizes. Plans and
source/action receipts survive reopening and approval continuation. Refusals remain
visible, and reply-only recovery does not restart research. Forgetting scrubs the
plan with the existing message lifecycle. This is bounded snippet research; full
page reading and live-search verification are not implied.

Provider attempts within the bound research turn are now recorded separately,
without prompts or responses. Turn totals sum planning, query selection, synthesis
and reply recovery. Failed, interrupted or unreported attempts keep affected totals
unknown; they are never treated as free. Approval holds and completion preserve the
same totals. Steering-discarded calls are counted once through this ledger.
ToolGate service costs and pre-turn memory/context preparation are separate scopes;
these totals do not claim an all-services bill. Legacy runs have no complete attempt
ledger and cannot retroactively gain accurate totals.

Five focused accounting cases pass: summed multi-call totals after reopening,
failed-call recovery remaining unknown, independently known token totals, approval
pause/resume, and process interruption retaining a pending receipt. Integrated Pi
regression: 960 passed, 8 skipped, one existing test-client deprecation warning
in 295.70 seconds. Skipped live-service/host-specific checks are not proven; no
live provider/search proof is claimed.
No paid fallback was enabled and final dashboard wiring remains deferred.

Verification: 61 focused tests across web execution, selections, ordinary tool
turns, reply recovery and steering. Includes scope/missing-query failures, bounds,
approval, single-search ceiling, provider failure, Stop, lost-receipt recovery and
refusal. Tests use scripted providers and ToolGate doubles, not a live search service.

Deep research verification: 70 focused research/tool/recovery/steering tests pass, including stop during planning and forgetting. No live provider/search calls were made.
