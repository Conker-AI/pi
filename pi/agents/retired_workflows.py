"""The tables of two retired ways to draft a workflow, kept only so forgetting a chat clears them.

Before Conker drafted tools itself (`conker.draft_tool`, `pi.agents.tool_drafts`), Pi offered to
draft a workflow from the owner's message (`workflow_offers`) and the command line linked a draft
it made from a chat message to that message (`workflow_proposals`). Both reached the owner only
through the command line and the Inbox, and tool requests replace both, so their routes and Inbox
items are gone (2026-10-30). Installs made before then may still hold rows that name a chat and a
message (and a draft's name, taken from that chat). Dropping the tables would need a migration, so
they stay, nothing writes or lists them, and forgetting a chat deletes its rows here.
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_proposals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    draft_id TEXT NOT NULL,
    name TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
    state TEXT NOT NULL CHECK(state IN ('open', 'published', 'dismissed')),
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS workflow_proposals_open ON workflow_proposals(state, created_at);
CREATE TABLE IF NOT EXISTS workflow_offers (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    message_id TEXT NOT NULL UNIQUE REFERENCES messages(id),
    state TEXT NOT NULL CHECK(state IN ('open', 'accepted', 'dismissed')),
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS workflow_offers_open ON workflow_offers(state, created_at);
"""


def initialize(db):
    db.executescript(SCHEMA)


def redact(db, session_ids: list[str]) -> None:
    """Forget with the chat, inside the caller's forgetting transaction."""
    if not session_ids:
        return
    chats = [(value,) for value in session_ids]
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_proposals'"
    ).fetchone():
        db.executemany("DELETE FROM workflow_proposals WHERE session_id=?", chats)
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_offers'"
    ).fetchone():
        db.executemany("DELETE FROM workflow_offers WHERE session_id=?", chats)
