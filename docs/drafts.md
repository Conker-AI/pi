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

Attachment drafts, send-time draft consumption, and browser gateway/frontend
integration are separate work; this increment supplies independent storage/API.
