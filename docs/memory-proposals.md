# Reviewed memory corrections

Pi's owner-only `/memory-proposals` API stores an exact proposed text correction
against a fetched MemoryGate record and revision. It does not infer approval from
the candidate, its confidence, or ordinary chat text. Create/list/get show the
old text, new text, reason, stated/inferred basis and linked owner-message IDs.
Only messages in the proposal's session may serve as evidence. Memory-disabled
sources cannot create or apply a proposal.

`POST /memory-proposals/{id}/decision` takes `apply` or `reject`. Pending content
is immutable; revising it means creating a new proposal. Applying claims the
proposal once, then sends only the reviewed text, target ID and expected memory
revision. Newer remote edits return a conflict rather than being overwritten.
Rejected proposals never dispatch. Repeating an approval does not resend it.

A timeout or process interruption leaves an unknown outcome.
`POST /memory-proposals/{id}/reconcile` reads the remote receipt without repeating
the write. Even a missing receipt does not prove an in-flight request cannot
commit. Receipts are checked against the proposal, namespace and exact revision
transition. Indexing degradation is retained separately from SQL edit success.

Configure this capability independently:

- `PI_MEMORY_CORRECTION_URL`: MemoryGate base URL.
- `PI_MEMORY_CORRECTION_KEY`: dedicated correction key, at least 32 characters.
- `PI_MEMORY_CORRECTION_AGENT_ID`: the fixed remote namespace.

MemoryGate uses matching `MEMORYGATE_CORRECTION_KEY` and
`MEMORYGATE_CORRECTION_AGENT_ID`. This is a dedicated correction capability, not
an admin credential, ingestion key or read key. Pi never forwards it to a browser
or a model. Missing configuration is explicit; partial configuration fails
startup. Nothing enables the capability or provisions secrets automatically.

Retention is explicit in proposal views: after approval, the corrected memory is
an independently reviewed MemoryGate record. Forgetting the source conversation
scrubs Pi's proposal text, baseline and reason, but does **not** undo that manual
memory edit. Remove the saved memory through MemoryGate to remove it as well.
Existing conversation evidence ingestion and incognito behavior remain unchanged;
this API neither converts every conversation into a proposal nor promotes model
inferences automatically. Correction preserves the memory's original source type
and confidence; proposal basis describes the review evidence, not a truth guarantee.

Tests use temporary SQLite databases and synthetic HTTP transports. They cover
parallel approval, stale edits, timeout/reconciliation, restart, privacy, rejection,
owner authorization and physical proposal forgetting. No live edits, final
dashboard wiring or deployment are part of this increment.
