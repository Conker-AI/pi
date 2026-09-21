"""Owner draft storage; drafts never enter model context or memory ingestion."""

import time
from typing import Literal

from pydantic import Field

from .agents import StrictModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_drafts (
 session_id TEXT NOT NULL REFERENCES sessions(id), scope TEXT NOT NULL,
 revision INTEGER NOT NULL, text TEXT NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(session_id,scope)
);
CREATE TABLE IF NOT EXISTS submitted_drafts (
 request_id TEXT PRIMARY KEY REFERENCES turn_submissions(request_id),
 session_id TEXT NOT NULL, scope TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_research (
 session_id TEXT NOT NULL, scope TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('web','deep')),
 PRIMARY KEY(session_id,scope),
 FOREIGN KEY(session_id,scope) REFERENCES conversation_drafts(session_id,scope) ON DELETE CASCADE
);
"""


class Save(StrictModel):
    expected_revision: int = Field(ge=0)
    text: str = Field(max_length=100000)
    research_mode: Literal["off", "web", "deep"] = "off"


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
    result = dict(row) if row else {"revision": 0, "text": "", "updated_at": None}
    mode = db.execute("SELECT mode FROM draft_research WHERE session_id=? AND scope=?",
                      (session_id, scope)).fetchone()
    if mode:
        result["research_mode"] = mode[0]
    return result


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
        db.execute("DELETE FROM draft_research WHERE session_id=? AND scope=?", (session_id, scope))
        if body.research_mode != "off":
            db.execute("INSERT INTO draft_research VALUES(?,?,?)", (session_id, scope, body.research_mode))
        result = _read(db, session_id, scope)
        db.commit()
        return result


def redact(db, session_ids):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_drafts'"
    ).fetchone():
        return
    for identity in session_ids:
        db.execute("DELETE FROM draft_research WHERE session_id=?", (identity,))
        db.execute("DELETE FROM conversation_drafts WHERE session_id=?", (identity,))
        db.execute("DELETE FROM submitted_drafts WHERE session_id=?", (identity,))


def reserve(db, request_id, session_id, task_id, revision, text, research_mode="off"):
    scope = _scope(db, session_id, task_id)
    draft = _read(db, session_id, scope)
    if (
        type(revision) is not int
        or revision < 1
        or draft["revision"] != revision
        or draft["text"] != text
        or draft.get("research_mode", "off") != research_mode
    ):
        raise DraftError("Submitted text or research mode no longer matches the saved draft revision.")
    db.execute(
        "INSERT INTO submitted_drafts VALUES(?,?,?,?)", (request_id, session_id, scope, revision)
    )


def consume(db, request_id):
    row = db.execute("SELECT * FROM submitted_drafts WHERE request_id=?", (request_id,)).fetchone()
    if row:
        current = _read(db, row["session_id"], row["scope"])
        if current["revision"] == row["revision"]:
            db.execute("DELETE FROM draft_research WHERE session_id=? AND scope=?",
                       (row["session_id"], row["scope"]))
        db.execute(
            "UPDATE conversation_drafts SET text='',revision=revision+1,updated_at=? "
            "WHERE session_id=? AND scope=? AND revision=?",
            (time.time(), row["session_id"], row["scope"], row["revision"]),
        )
