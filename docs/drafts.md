# Conversation drafts

Pi stores a text draft per conversation, with separate task-prompt drafts keyed by
the task's authoritative conversation. Owner-only `/drafts/{session_id}` GET/PUT
accepts an optional `task_id`. PUT requires `expected_revision`; conflicting writes
return 409 rather than overwriting another window. Saving empty text clears a
draft while retaining its revision, so stale clients cannot resurrect it.

Drafts survive restart but are never messages, model input, or MemoryGate ingestion.
Offline forgetting deletes drafts for the forgotten session tree and physically
scrubs database/WAL copies. Forgotten or nonexistent sessions cannot read/write
drafts. Task prompts cannot cross conversation boundaries.

Turn requests optionally carry `draft_revision`. Reservation verifies that exact
revision and text; binding the input message clears it atomically. Failed
preparation preserves the draft. New text saved during preparation survives, and
replayed submissions cannot clear it. Task-bound requests consume only their task
draft. Omitting the revision leaves drafts unchanged for existing clients.

Attachment drafts and browser gateway/frontend integration remain separate work.
