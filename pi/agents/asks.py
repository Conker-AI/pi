"""Conker's questions to the owner: asked in a chat, answered in his Inbox.

`conker.ask` files one question with 2-4 options Conker writes (Yes / No when it gives none). The
turn that asked ends at once; the chat is never held. His answer, an option or his own words,
arrives later as his message in that chat and starts a new turn there (`pi.turns.answers`).

An option is text the AI wrote, perhaps after reading someone else's words, so an answer given by
tapping one is marked: it never counts as his own typed request approving a matching action
(`pi.turns.access`). Asking is always allowed, but at most `PER_CHAT` questions wait per chat and
`OPEN_LIMIT` in all, so outside content cannot flood his Inbox.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from pi.conversation import settings as session_settings
from pi.core.errors import ServiceError

ASK_ID = r"^ask_[a-f0-9]{32}$"
PER_CHAT = 3
OPEN_LIMIT = 20
DEFAULT_OPTIONS = ["Yes", "No"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_asks (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), turn_id TEXT,
 question TEXT NOT NULL, options TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','answered','dismissed')),
 answer TEXT, chosen INTEGER, answer_request TEXT, created_at REAL NOT NULL, answered_at REAL
);
CREATE INDEX IF NOT EXISTS owner_asks_open ON owner_asks(state, created_at);
CREATE INDEX IF NOT EXISTS owner_asks_request ON owner_asks(answer_request);
"""


class AskError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def options(value) -> list[str]:
    """2-4 short, distinct options, or Yes / No when the model gave none that fit."""
    if isinstance(value, str):
        value = re.split(r"\s*[/|\n]\s*", value)
    if not isinstance(value, list):
        return list(DEFAULT_OPTIONS)
    cleaned = []
    for item in value:
        text = " ".join(item.split())[:60] if isinstance(item, str) else ""
        if text and text.lower() not in {seen.lower() for seen in cleaned}:
            cleaned.append(text)
    return cleaned[:4] if len(cleaned) >= 2 else list(DEFAULT_OPTIONS)


def _title(db, session_id):
    privacy = session_settings.source_privacy(db, session_id)
    if privacy is not None and any(privacy.values()):
        return "Private chat"
    row = db.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
    return (row["title"] if row else None) or "Conversation"


def _view(db, row) -> dict:
    return {
        "id": row["id"],
        "sessionId": row["session_id"],
        "chatTitle": _title(db, row["session_id"]),
        "question": row["question"],
        "options": json.loads(row["options"]),
        "state": row["state"],
        "answer": row["answer"],
        "chosen": bool(row["chosen"]) if row["chosen"] is not None else None,
        "createdAt": row["created_at"],
        "answeredAt": row["answered_at"],
    }


def file(store, session_id, turn_id, question, choices, now=None) -> dict:
    """Record one question. A ValueError is an answer for the model, not a failure."""
    question = " ".join(str(question or "").split())[:500]
    if not question:
        raise ValueError("Give the question as text.")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        waiting = db.execute(
            "SELECT COUNT(*) FROM owner_asks WHERE state='open' AND session_id=?", (session_id,)
        ).fetchone()[0]
        if waiting >= PER_CHAT:
            raise ValueError(
                f"{waiting} questions from this chat already wait for the owner. Do not ask "
                "another; wait for his answers."
            )
        if db.execute("SELECT COUNT(*) FROM owner_asks WHERE state='open'").fetchone()[0] >= (
            OPEN_LIMIT
        ):
            raise ValueError("The owner's Inbox already holds many questions. Do not ask now.")
        identity = "ask_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO owner_asks VALUES (?,?,?,?,?,'open',NULL,NULL,NULL,?,NULL)",
            (
                identity,
                session_id,
                turn_id,
                question,
                json.dumps(options(choices), ensure_ascii=False),
                time.time() if now is None else now,
            ),
        )
        view = _view(db, db.execute("SELECT * FROM owner_asks WHERE id=?", (identity,)).fetchone())
        db.commit()
        return view


def _row(db, identity):
    if not re.fullmatch(ASK_ID, identity or ""):
        raise AskError("not_found", "Question not found.", 404)
    row = db.execute(
        "SELECT * FROM owner_asks WHERE id=? AND session_id NOT IN "
        "(SELECT session_id FROM forgotten_sessions)",
        (identity,),
    ).fetchone()
    if row is None:
        raise AskError("not_found", "Question not found.", 404)
    return row


def get(store, identity) -> dict:
    with store._connect() as db:
        return _view(db, _row(db, identity))


def listing(store, limit=50) -> list[dict]:
    """Questions waiting for him, oldest first."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM owner_asks WHERE state='open' AND session_id NOT IN "
            "(SELECT session_id FROM forgotten_sessions) ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [_view(db, row) for row in rows]


def answer(store, identity, text, *, chosen, now=None) -> dict:
    """Record his answer once. `chosen`: he tapped one of Conker's options (not his own words)."""
    text = str(text or "").strip()[:4000]
    if not text:
        raise AskError("empty_answer", "Write an answer or pick an option.", 422)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        if row["state"] != "open":
            raise AskError("decided", "This question was already answered.", 409)
        if chosen and text not in json.loads(row["options"]):
            raise AskError("unknown_option", "Pick one of the options shown.", 422)
        db.execute(
            "UPDATE owner_asks SET state='answered', answer=?, chosen=?, answered_at=? WHERE id=?",
            (text, int(bool(chosen)), time.time() if now is None else now, identity),
        )
        view = _view(db, _row(db, identity))
        db.commit()
        return view


def dismiss(store, identity, now=None) -> dict:
    """He doesn't want to answer: nothing goes back to the chat."""
    with store._connect() as db:
        changed = db.execute(
            "UPDATE owner_asks SET state='dismissed', answered_at=? WHERE id=? AND state='open'",
            (time.time() if now is None else now, identity),
        ).rowcount
    if not changed:
        get(store, identity)
        raise AskError("decided", "This question was already answered.", 409)
    return get(store, identity)


def delivering(store, identity, request_id) -> None:
    """The submission his answer is sent as; written before that turn starts."""
    with store._connect() as db:
        db.execute("UPDATE owner_asks SET answer_request=? WHERE id=?", (request_id, identity))


def answer_words(db, turn_id) -> str | None:
    """His own words in a turn that carries his answer to a question: None when the turn is not
    an answer, "" when he tapped one of Conker's options (those words are the AI's, not his)."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_asks'"
    ).fetchone():
        return None
    row = db.execute(
        "SELECT a.answer, a.chosen FROM owner_asks a JOIN turn_submissions s "
        "ON s.request_id=a.answer_request WHERE s.turn_id=?",
        (turn_id,),
    ).fetchone()
    if row is None:
        return None
    return "" if row["chosen"] else row["answer"] or ""


def redact(db, session_ids: list[str]) -> None:
    """Forget with the chat: a question quotes what was said there."""
    if (
        not session_ids
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_asks'"
        ).fetchone()
    ):
        return
    db.executemany("DELETE FROM owner_asks WHERE session_id=?", [(value,) for value in session_ids])


def needs_you(store) -> list[dict]:
    return [
        {
            "kind": "ask",
            "id": item["id"],
            "title": item["question"],
            "since": item["createdAt"],
            "source": {"kind": "ask", "askId": item["id"], "sessionId": item["sessionId"]},
        }
        for item in listing(store)
    ]
