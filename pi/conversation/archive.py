"""Archived chats: the owner hides chats from the chat list and brings them back.

Archiving is a list state only. It changes no message, turn, memory or job, so it is reversible
and needs no password. One request sets the state of up to 100 chats in one transaction; a chat
that does not exist or was forgotten fails the whole request, so nothing is half done. Forgetting
a chat deletes its archive row with it.
"""

from __future__ import annotations

import time
from typing import Annotated

from pydantic import Field

from pi.core.errors import ServiceError
from pi.core.models import Strict

MAX_CHATS = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS session_archive (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), archived_at REAL NOT NULL
);
"""

ChatId = Annotated[str, Field(pattern=r"^ses_[0-9a-f]{16}$")]


class Archive(Strict):
    sessions: list[ChatId] = Field(min_length=1, max_length=MAX_CHATS)
    archived: bool


class ArchiveError(ServiceError):
    pass


def apply(store, body: Archive) -> dict:
    """Set every listed chat archived or active; a chat already in that state stays as it is."""
    ids = list(dict.fromkeys(body.sessions))
    marks = ",".join("?" * len(ids))
    now = time.time()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            found = {
                row[0]
                for row in db.execute(
                    "SELECT s.id FROM sessions s LEFT JOIN forgotten_sessions f "
                    f"ON f.session_id=s.id WHERE s.id IN ({marks}) "
                    "AND s.status!='forgotten' AND f.session_id IS NULL",
                    ids,
                )
            }
            if len(found) != len(ids):
                raise ArchiveError(
                    "not_found", "One of these chats no longer exists; nothing was changed.", 404
                )
            if body.archived:
                # A chat archived earlier keeps its first archive time.
                db.executemany(
                    "INSERT OR IGNORE INTO session_archive VALUES (?,?)",
                    [(identity, now) for identity in ids],
                )
            else:
                db.executemany(
                    "DELETE FROM session_archive WHERE session_id=?",
                    [(identity,) for identity in ids],
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return {"sessions": ids, "archived": body.archived}


def redact(db, session_ids) -> None:
    """Offline forgetting hook: a forgotten chat leaves no archive row; the caller owns the transaction."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='session_archive'").fetchone():
        db.executemany(
            "DELETE FROM session_archive WHERE session_id=?",
            [(identity,) for identity in session_ids],
        )
