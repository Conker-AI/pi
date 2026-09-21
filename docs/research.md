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

Deep research still reports `research_unavailable` before retrieval/generation.
Its bounded iterative plan, progress, collection and synthesis remain unfinished.
No paid fallback was enabled and final dashboard wiring remains deferred.

Verification: 61 focused tests across web execution, selections, ordinary tool
turns, reply recovery and steering. Includes scope/missing-query failures, bounds,
approval, single-search ceiling, provider failure, Stop, lost-receipt recovery and
refusal. Tests use scripted providers and ToolGate doubles, not a live search service.
