"""Owner draft storage; drafts never enter model context or memory ingestion."""

import time

from pydantic import Field

from .agents import StrictModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_drafts (
 session_id TEXT NOT NULL REFERENCES sessions(id), scope TEXT NOT NULL,
 revision INTEGER NOT NULL, text TEXT NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(session_id,scope)
);
"""


class Save(StrictModel):
    expected_revision: int = Field(ge=0)
    text: str = Field(max_length=100000)


class DraftError(ValueError):
    pass


def _scope(db, session_id, task_id):
    session = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if session is None or session[0] == "forgotten":
        raise DraftError("Conversation unavailable.")
    if task_id is None:
        return "chat"
    task = db.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None or task[0] != session_id:
        raise DraftError("Task does not belong to this conversation.")
    return "task:" + task_id


def _read(db, session_id, scope):
    row = db.execute(
        "SELECT revision,text,updated_at FROM conversation_drafts WHERE session_id=? AND scope=?",
        (session_id, scope),
    ).fetchone()
    return dict(row) if row else {"revision": 0, "text": "", "updated_at": None}


def load(store, session_id, task_id=None):
    with store._connect() as db:
        return _read(db, session_id, _scope(db, session_id, task_id))


def save(store, session_id, body, task_id=None):
    body = Save.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        scope = _scope(db, session_id, task_id)
        current = _read(db, session_id, scope)
        if current["revision"] != body.expected_revision:
            raise DraftError("Draft changed in another window; reload before saving.")
        # Empty drafts keep their revision to prevent stale writers resurrecting text.
        db.execute(
            "INSERT INTO conversation_drafts VALUES(?,?,?,?,?) "
            "ON CONFLICT(session_id,scope) DO UPDATE SET "
            "revision=excluded.revision,text=excluded.text,updated_at=excluded.updated_at",
            (session_id, scope, current["revision"] + 1, body.text, time.time()),
        )
        result = _read(db, session_id, scope)
        db.commit()
        return result


def redact(db, session_ids):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_drafts'"
    ).fetchone():
        return
    for identity in session_ids:
        db.execute("DELETE FROM conversation_drafts WHERE session_id=?", (identity,))
