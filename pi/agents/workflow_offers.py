"""Offers to draft a workflow that Pi made in chat before Conker drafted tools itself.

Pi used to put an offer in the Inbox when the owner's own message asked for something recurring.
Conker now drafts tools itself (`conker.draft_tool`, `pi.agents.tool_drafts`), so Pi makes no new
offers. The ones already made stay until the owner accepts (`conker workflow accept`, which drafts
from that message as `from-chat` does) or dismisses them. Pi keeps only the link and its state, and
forgets it with the chat.
"""

from __future__ import annotations

import time
import unicodedata

from pi.core.errors import ServiceError

SCHEMA = """
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

EXCERPT = 80


class OfferError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def _view(row) -> dict:
    return {
        "id": row["id"],
        "sessionId": row["session_id"],
        "messageId": row["message_id"],
        "state": row["state"],
        "createdAt": row["created_at"],
    }


def _excerpt(text) -> str:
    """The start of the owner's message on one line, without control characters."""
    flat = " ".join(
        "".join(c for c in str(text) if unicodedata.category(c)[0] != "C" or c in "\n\t").split()
    )
    return flat if len(flat) <= EXCERPT else flat[: EXCERPT - 1].rstrip() + "…"


def get(store, identity: str) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT * FROM workflow_offers WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise OfferError("not_found", "Workflow offer not found.", 404)
    return _view(row)


def open_items(store) -> list[dict]:
    """Open offers whose message is still there, with the start of that message."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM workflow_offers WHERE state='open' "
            "AND session_id NOT IN (SELECT session_id FROM forgotten_sessions) ORDER BY created_at"
        ).fetchall()
    items = []
    for row in rows:
        message = store.get_message(row["message_id"])
        if (
            message is None
            or message.get("content_status") == "forgotten"
            or not isinstance(message.get("content"), str)
        ):
            continue
        items.append({**_view(row), "excerpt": _excerpt(message["content"])})
    return items


def needs_you(store) -> list[dict]:
    return [
        {
            "kind": "workflow-offer",
            "id": item["id"],
            "title": "Draft a workflow from your message? " + item["excerpt"],
            "since": item["createdAt"],
            "source": {
                "kind": "workflow-offer",
                "offerId": item["id"],
                "sessionId": item["sessionId"],
                "messageId": item["messageId"],
            },
        }
        for item in open_items(store)
    ]


def accepted(db, message_id: str, now: float) -> None:
    """A draft was made from this message: its offer is done. Runs in the caller's transaction."""
    db.execute(
        "UPDATE workflow_offers SET state='accepted', decided_at=? "
        "WHERE message_id=? AND state='open'",
        (now, message_id),
    )


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    offer = get(store, identity)
    with store._connect() as db:
        db.execute(
            "UPDATE workflow_offers SET state='dismissed', decided_at=? WHERE id=? AND state='open'",
            (time.time() if now is None else now, identity),
        )
        db.commit()
    return {"id": identity, "state": "dismissed" if offer["state"] == "open" else offer["state"]}


def redact(db, session_ids: list[str]) -> None:
    """Forget with the chat, inside the caller's forgetting transaction."""
    if (
        session_ids
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_offers'"
        ).fetchone()
    ):
        db.executemany(
            "DELETE FROM workflow_offers WHERE session_id=?", [(value,) for value in session_ids]
        )
