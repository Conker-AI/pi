"""Workflow drafts made from a chat message, waiting in the Inbox for the owner.

The owner turns one of their own messages into a workflow: the command line (later a button)
drafts it with the builder and saves it in ToolGate's editor, then records it here so the Inbox
shows it until the owner publishes or dismisses it. Pi keeps only the link and the draft's name,
never runs or publishes anything, and forgets the record with the chat.
"""

from __future__ import annotations

import time
import uuid

from pydantic import Field

from pi.core.errors import ServiceError
from pi.core.models import Strict

IDENTIFIER = r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"
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
"""


class Record(Strict):
    sessionId: str = Field(min_length=1, max_length=128)
    messageId: str = Field(min_length=1, max_length=128)
    draftId: str = Field(pattern=IDENTIFIER)
    name: str = Field(min_length=1, max_length=100)
    valid: bool


class Published(Strict):
    draftId: str = Field(pattern=IDENTIFIER)


class ProposalError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def _view(row) -> dict:
    return {
        "id": row["id"],
        "sessionId": row["session_id"],
        "messageId": row["message_id"],
        "draftId": row["draft_id"],
        "name": row["name"],
        "valid": bool(row["valid"]),
        "state": row["state"],
        "createdAt": row["created_at"],
    }


def record(store, body: Record, *, now: float | None = None) -> dict:
    """Link one of the owner's own messages to the draft made from it."""
    session = store.get_session(body.sessionId)
    message = store.get_message(body.messageId)
    if (
        session is None
        or session.get("status") == "forgotten"
        or message is None
        or message["session_id"] != body.sessionId
    ):
        raise ProposalError("not_found", "That message is not in that chat.", 404)
    if message["role"] != "user" or message.get("content_status") == "forgotten":
        raise ProposalError("not_owner_message", "Make workflows only from your own messages.", 409)
    identity = "wfp_" + uuid.uuid4().hex
    with store._connect() as db:
        db.execute(
            "INSERT INTO workflow_proposals VALUES (?,?,?,?,?,?,'open',?,NULL)",
            (
                identity,
                body.sessionId,
                body.messageId,
                body.draftId,
                body.name.strip(),
                int(body.valid),
                time.time() if now is None else now,
            ),
        )
        db.commit()
        row = db.execute("SELECT * FROM workflow_proposals WHERE id=?", (identity,)).fetchone()
    return _view(row)


def open_items(store) -> list[dict]:
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM workflow_proposals WHERE state='open' "
            "AND session_id NOT IN (SELECT session_id FROM forgotten_sessions) ORDER BY created_at"
        ).fetchall()
    return [_view(row) for row in rows]


def needs_you(store) -> list[dict]:
    """Open drafts for the Inbox; an invalid one says it needs fixing."""
    return [
        {
            "kind": "workflow",
            "id": item["id"],
            "title": ("Workflow draft ready: " if item["valid"] else "Workflow draft to fix: ")
            + item["name"],
            "since": item["createdAt"],
            "draftId": item["draftId"],
            "source": {"kind": "workflow-proposal", "proposalId": item["id"]},
        }
        for item in open_items(store)
    ]


def _decide(store, where: str, values: tuple, state: str, now: float | None) -> int:
    with store._connect() as db:
        changed = db.execute(
            f"UPDATE workflow_proposals SET state=?, decided_at=? WHERE state='open' AND {where}",
            (state, time.time() if now is None else now, *values),
        ).rowcount
        db.commit()
    return changed


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT state FROM workflow_proposals WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ProposalError("not_found", "Workflow proposal not found.", 404)
    _decide(store, "id=?", (identity,), "dismissed", now)
    return {"id": identity, "state": "dismissed" if row["state"] == "open" else row["state"]}


def published(store, body: Published, *, now: float | None = None) -> dict:
    """The owner published the draft: its Inbox items are done."""
    return {
        "draftId": body.draftId,
        "closed": _decide(store, "draft_id=?", (body.draftId,), "published", now),
    }


def redact(db, session_ids: list[str]) -> None:
    """Forget with the chat, inside the caller's forgetting transaction: the name came from it."""
    if (
        session_ids
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_proposals'"
        ).fetchone()
    ):
        db.executemany(
            "DELETE FROM workflow_proposals WHERE session_id=?", [(value,) for value in session_ids]
        )
