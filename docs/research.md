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

## Implementation boundary

The bounded research executor is not implemented yet. Web/deep requests currently
fail preparation with `research_unavailable` before retrieval or generation.
The receipt and requested choice remain inspectable; retrying the same request ID
only reads its state. No ordinary answer is silently substituted.

Next: reuse ToolGate research capabilities through normal scoped action receipts;
add bounded planning, collection, synthesis, provenance, cancellation and recovery.
Do not enable paid fallbacks, duplicate the search engine, or claim that a fixed
multi-source bundle is an iterative deep-research run. Answer retries must use
saved evidence rather than repeat searches. Final dashboard wiring is deferred.
