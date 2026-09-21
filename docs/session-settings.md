# Session privacy and agent execution selections

Admin-only GET `/sessions/{id}/settings` returns `{revision, settings}`. POST accepts
`{expected_revision, settings}`, with strict settings of this shape:

```json
{
  "agentId": "companion",
  "privacy": {"memoryDisabled": false, "harnessDisabled": false}
}
```

Unsaved existing sessions have revision 0 and the original Companion behavior.
Saving requires an open session, matching revision and an active agent. Preparing,
running or unresolved action/reply work blocks changes. Gateway credentials cannot
access these routes; browser wiring is not part of this package.

Submission reservation captures the exact settings and agent configuration/version
in the same transaction. Binding a turn retains that snapshot. Replays and resumes
use the original selection, not later profile edits. Forks inherit settings,
including reviewed forks. Every inserted message receives immutable privacy and
ingestion eligibility metadata in the transcript transaction.

**Privacy changes apply to future work.** No-memory disables memory retrieval and
ingestion for excluded messages. Startup backfills cannot add excluded messages to
the outbox, and delivery rechecks eligibility before contacting MemoryGate. Turning
memory back on does not retrospectively ingest excluded messages. Previously allowed
pending deliveries remain allowed; this API does not cancel them or erase previously
stored memories. Use the explicit forgetting workflow for retrospective deletion.

No-harness prevents automatic model summarization, including automatic forks.
Overlong histories require an owner-reviewed fork. The existing deterministic
answer router remains available, including hosted answers: this privacy flag does
not require a local model and does not disable ordinary tools. Auxiliary model
routing/retrieval remains excluded; per-message `retrieve` policies remain blocked
by the existing context planner until an authorized resolver exists.

Specialist execution now receives the frozen instructions and only the intersection
of selected tool IDs and current ToolGate availability. Approval resumes check that
intersection before a new invocation; reconciliation of an uncertain prior action
remains possible without dispatching it again. ToolGate still owns authorization.
An explicit agent model ID resolves through the frozen model-role catalogue and
server-owned adapters. Missing mappings fail closed; manual overrides never silently
fall back. See [model-roles.md](model-roles.md) for dispatch and evidence details.

Specialist memory currently stays disabled: a stored agent ID is not an authorized
MemoryGate namespace mapping. Companion sessions with saved settings use the scoped
conversation context endpoint; legacy unsaved sessions retain the existing retrieval
behavior. `MemoryClient.retrieve` supports `none`, `conversation` and `selected`
filters and rejects restricted responses without the matching `scope` marker, so an
older MemoryGate cannot silently broaden the request. Scoped MemoryGate contracts
must be present before those reads succeed.

`session_settings.source_privacy(db, session_id)` supports internal source resolvers.
It returns explicit booleans plus derived `incognito`, or None for unavailable or
forgotten sessions. It conservatively combines current and historical message
privacy so a future-turn setting change cannot expose an older private source.

Validation uses temporary databases and stub providers:

```sh
python -m pytest tests/test_session_settings.py tests/test_memory.py tests/test_loop.py tests/test_submissions.py tests/test_tool_turns.py tests/test_context_controls.py -q
```
