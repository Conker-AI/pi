"""Archived and trashed chats: the owner hides chats from the chat list and brings them back.

Both are list states only. They change no message, turn, memory or job, so they are reversible
and need no password. An archived chat stays readable and searchable; a trashed chat is also left
out of search and memory recall until it is restored or deleted forever (forget_queue). One
request sets the state of up to 100 chats in one transaction; a chat that does not exist or was
forgotten fails the whole request, so nothing is half done. Forgetting a chat deletes both rows.

A chat made from a chat in the Trash (sessions.parent_id, all the way up) is hidden with it
(pi.conversation.visibility), so restoring it alone would take it out of the Trash view and leave
it listed nowhere: that restore is refused (`restore_parent_first`, naming the chat to restore
first). Restoring both in one request works in either order.
"""

from __future__ import annotations

import time
from typing import Annotated

from pydantic import Field

from pi.conversation import forget_queue
from pi.core.errors import ServiceError
from pi.core.models import Strict

MAX_CHATS = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS session_archive (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), archived_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_trash (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), trashed_at REAL NOT NULL
);
"""

ChatId = Annotated[str, Field(pattern=r"^ses_[0-9a-f]{16}$")]


class Archive(Strict):
    sessions: list[ChatId] = Field(min_length=1, max_length=MAX_CHATS)
    archived: bool


class Trash(Strict):
    sessions: list[ChatId] = Field(min_length=1, max_length=MAX_CHATS)
    trashed: bool


class ArchiveError(ServiceError):
    pass


def apply(store, body: Archive) -> dict:
    """Set every listed chat archived or active; a chat already in that state stays as it is."""
    ids = _mark(store, body.sessions, "session_archive", body.archived)
    return {"sessions": ids, "archived": body.archived}


def trash(store, body: Trash) -> dict:
    """Move every listed chat to the trash or restore it; restoring keeps its archive state.

    A chat queued to be deleted forever cannot be restored: the deletion goes ahead."""
    if body.trashed:
        ids = _mark(store, body.sessions, "session_trash", True)
        return {"sessions": ids, "trashed": True}
    with forget_queue.queue_lock:
        pending = sorted(forget_queue.pending_sessions(store.path) & set(body.sessions))
        if pending:
            raise ArchiveError(
                "deletion_pending",
                "These chats are being deleted forever and cannot be restored.",
                409,
                sessions=pending,
            )
        ids = _mark(store, body.sessions, "session_trash", False)
    return {"sessions": ids, "trashed": False}


def hide(store, session_ids) -> None:
    """Put chats queued for deletion in the trash; chats that are gone already are skipped."""
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO session_trash "
                f"SELECT s.id, ? FROM sessions s WHERE s.id IN ({marks}) "
                "AND s.status!='forgotten'",
                [time.time(), *ids],
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def trashed(db, session_ids) -> set[str]:
    """The listed chats that are in the trash (none before the table exists)."""
    ids = list(session_ids)
    if (
        not ids
        or not db.execute("SELECT 1 FROM sqlite_master WHERE name='session_trash'").fetchone()
    ):
        return set()
    marks = ",".join("?" * len(ids))
    query = f"SELECT session_id FROM session_trash WHERE session_id IN ({marks})"
    return {row[0] for row in db.execute(query, ids)}


def _mark(store, sessions, table: str, on: bool) -> list[str]:
    ids = list(dict.fromkeys(sessions))
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
            if table == "session_trash" and not on:
                _refuse_hidden_parents(db, ids)
            if on:
                # A chat marked earlier keeps its first time.
                db.executemany(
                    f"INSERT OR IGNORE INTO {table} VALUES (?,?)",
                    [(identity, now) for identity in ids],
                )
            else:
                db.executemany(
                    f"DELETE FROM {table} WHERE session_id=?",
                    [(identity,) for identity in ids],
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return ids


def _refuse_hidden_parents(db, ids) -> None:
    """Refuse restoring a chat while a chat it was made from is still in the Trash, unless that
    chat is restored in the same request; the caller's transaction is rolled back."""
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        "WITH RECURSIVE up(child, id) AS ("
        f"SELECT id, parent_id FROM sessions WHERE id IN ({marks}) "
        "UNION SELECT up.child, s.parent_id FROM up JOIN sessions s ON s.id=up.id) "
        "SELECT up.child, up.id FROM up JOIN session_trash t ON t.session_id=up.id "
        f"WHERE up.id NOT IN ({marks}) ORDER BY up.child, up.id",
        [*ids, *ids],
    ).fetchall()
    if rows:
        raise ArchiveError(
            "restore_parent_first",
            "These chats were made from a chat that is still in the Trash; restore that chat "
            "first, or both together. Nothing was changed.",
            409,
            sessions=sorted({row[0] for row in rows}),
            parents=sorted({row[1] for row in rows}),
        )


def redact(db, session_ids) -> None:
    """Offline forgetting hook: a forgotten chat leaves no archive or trash row; the caller owns the transaction."""
    for table in ("session_archive", "session_trash"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            db.executemany(
                f"DELETE FROM {table} WHERE session_id=?",
                [(identity,) for identity in session_ids],
            )
