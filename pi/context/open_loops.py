"""Open loops (R-M7): what the owner promised and what Conker promised, across every chat.

A loop is one promise: its words, who promised (`owner` or `conker`), an optional due date in the
owner's time zone, the chat it belongs to and, when it came from a message, that message. States:
open, done, dropped; any state can be set again (a loop marked done by mistake reopens).

Loops have their own table, not a kind of the chat's context items: a loop outlives the chat
going `forked` or `closed` (an automatic rollover must not strand it), is listed across chats,
never enters the chat's frozen settings snapshot (so writing one pauses no queued message) and has
its own cap. A loop in a hidden chat (forgotten or in the Trash, `visibility.hidden_sql`) is
neither listed nor changed; forgetting the chat deletes it (`redact`).

Design: context/design/open-loops.md in the Conker workspace.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import Field

from pi.conversation import visibility
from pi.core.errors import ServiceError
from pi.core.models import Strict
from pi.owner import preferences as prefs

SCHEMA = """
CREATE TABLE IF NOT EXISTS open_loops (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 message_id TEXT REFERENCES messages(id), text TEXT NOT NULL,
 promised_by TEXT NOT NULL CHECK(promised_by IN ('owner','conker')), due_on TEXT,
 state TEXT NOT NULL CHECK(state IN ('open','done','dropped')), revision INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS open_loops_listed ON open_loops(state, due_on, created_at, id);
CREATE INDEX IF NOT EXISTS open_loops_session ON open_loops(session_id);
"""

# Open loops at once, across every chat: a runaway writer cannot fill the table.
MAX_OPEN = 1000
MAX_TEXT = 500
QUOTE_CHARS = 300
LOOP_ID = r"^loop_[0-9a-f]{32}$"
# Sorts after every real date, so undated loops come last.
UNDATED = "9999-12-31"
NOT_THERE = "That open loop is not there."
BAD_CURSOR = "Use the nextAfter value the last page gave."


class LoopError(ServiceError):
    pass


class LoopAdd(Strict):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    promised_by: Literal["owner", "conker"] = "owner"
    # YYYY-MM-DD in the owner's time zone; a strict model takes dates as text from JSON.
    due_on: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    message_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")


class LoopState(Strict):
    state: Literal["open", "done", "dropped"]
    # Given, it must match; left out, the change applies to whatever revision is current.
    expected_revision: int | None = Field(default=None, ge=1, le=2147483646)


class Loop(Strict):
    id: str
    sessionId: str
    sessionTitle: str
    messageId: str | None
    quote: str | None
    text: str
    promisedBy: Literal["owner", "conker"]
    dueOn: str | None
    overdue: bool
    state: Literal["open", "done", "dropped"]
    revision: int
    createdAt: float
    updatedAt: float


class LoopPage(Strict):
    today: str
    timeZone: str
    items: list[Loop]
    nextAfter: str | None


class LoopReceipt(Strict):
    loop: Loop


def _today(db) -> tuple[str, str]:
    """The owner's local date and zone (the zone the quiet hours use)."""
    row = db.execute("SELECT preferences FROM owner_preferences WHERE singleton=1").fetchone()
    preferences = prefs.OwnerPreferences.model_validate_json(row["preferences"])
    return prefs.local_day(preferences, datetime.now(UTC)), preferences.quietHours.timeZone


def _clean(text: str) -> str:
    cleaned = text.strip()
    if not 1 <= len(cleaned) <= MAX_TEXT:
        raise LoopError("invalid_text", f"Give the promise as 1-{MAX_TEXT} characters.", 422)
    if any(ord(c) < 32 and c != "\n" for c in cleaned):
        raise LoopError("invalid_text", "The promise has a control character.", 422)
    return cleaned


def _due(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise LoopError(
            "invalid_date", "Give the due date as a real YYYY-MM-DD date.", 422
        ) from None


def _hidden_filter() -> str:
    return f"s.status<>'forgotten' AND NOT {visibility.hidden_sql('l.session_id')}"


def _visible_session(db, session_id: str) -> None:
    row = db.execute(
        f"SELECT 1 FROM sessions WHERE id=? AND status<>'forgotten' "
        f"AND NOT {visibility.hidden_sql('id')}",
        (session_id,),
    ).fetchone()
    if row is None:
        raise LoopError("not_found", "Conversation unavailable.", 404)


def _check_message(db, session_id: str, message_id: str) -> None:
    row = db.execute(
        "SELECT 1 FROM messages WHERE id=? AND session_id=?", (message_id, session_id)
    ).fetchone()
    if row is None:
        raise LoopError("outside_boundary", "The message is not part of this conversation.", 422)


_SELECT = f"""
SELECT l.id, l.session_id, s.title, l.message_id, l.text, l.promised_by, l.due_on, l.state,
       l.revision, l.created_at, l.updated_at,
       CASE WHEN json_valid(m.content) AND json_type(m.content)='text'
            THEN substr(json_extract(m.content,'$'), 1, {QUOTE_CHARS}) END AS quote
FROM open_loops l JOIN sessions s ON s.id=l.session_id
LEFT JOIN messages m ON m.id=l.message_id
"""


def _view(row, today: str) -> dict:
    due = row["due_on"]
    return Loop(
        id=row["id"],
        sessionId=row["session_id"],
        sessionTitle=row["title"],
        messageId=row["message_id"],
        quote=row["quote"],
        text=row["text"],
        promisedBy=row["promised_by"],
        dueOn=due,
        overdue=row["state"] == "open" and due is not None and due < today,
        state=row["state"],
        revision=row["revision"],
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
    ).model_dump()


def _one(db, loop_id: str, today: str) -> dict:
    row = db.execute(f"{_SELECT} WHERE l.id=? AND {_hidden_filter()}", (loop_id,)).fetchone()
    if row is None:
        raise LoopError("not_found", NOT_THERE, 404)
    return _view(row, today)


def add(store, body: LoopAdd) -> dict:
    """File one loop for a visible chat; the message, when named, must be that chat's."""
    body = LoopAdd.model_validate(body.model_dump())
    text = _clean(body.text)
    due_on = _due(body.due_on)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _visible_session(db, body.session_id)
        if body.message_id is not None:
            _check_message(db, body.session_id, body.message_id)
        count = db.execute("SELECT COUNT(*) FROM open_loops WHERE state='open'").fetchone()[0]
        if count >= MAX_OPEN:
            raise LoopError(
                "too_many", f"There are already {MAX_OPEN} open loops; close some first.", 409
            )
        now = time.time()
        loop_id = "loop_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO open_loops(id,session_id,message_id,text,promised_by,due_on,state,"
            "revision,created_at,updated_at) VALUES (?,?,?,?,?,?,'open',1,?,?)",
            (
                loop_id,
                body.session_id,
                body.message_id,
                text,
                body.promised_by,
                due_on,
                now,
                now,
            ),
        )
        today, _ = _today(db)
        loop = _one(db, loop_id, today)
        db.commit()
    return {"loop": loop}


def change_state(store, loop_id: str, body: LoopState) -> dict:
    """Mark a loop done, dropped or open again; the chat may be forked or closed."""
    body = LoopState.model_validate(body.model_dump())
    if not re.fullmatch(LOOP_ID, loop_id):
        raise LoopError("not_found", NOT_THERE, 404)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        today, _ = _today(db)
        current = _one(db, loop_id, today)
        if body.expected_revision is not None and body.expected_revision != current["revision"]:
            raise LoopError(
                "revision_conflict",
                "This open loop changed; read it again.",
                409,
                revision=current["revision"],
            )
        if body.state != current["state"]:
            db.execute(
                "UPDATE open_loops SET state=?, revision=revision+1, updated_at=? WHERE id=?",
                (body.state, time.time(), loop_id),
            )
            current = _one(db, loop_id, today)
        db.commit()
    return {"loop": current}


def listing(store, state: str, due_before: date | None, limit: int, after: str | None) -> dict:
    """One page of loops in visible chats: soonest due first, undated last, then oldest."""
    if after is not None and not re.fullmatch(LOOP_ID, after):
        raise LoopError("invalid_cursor", BAD_CURSOR, 422)
    where = [_hidden_filter()]
    params: list = []
    if state != "all":
        where.append("l.state=?")
        params.append(state)
    if due_before is not None:
        where.append("l.due_on IS NOT NULL AND l.due_on<?")
        params.append(due_before.isoformat())
    order = f"COALESCE(l.due_on,'{UNDATED}'), l.created_at, l.id"
    with store._connect() as db:
        today, zone = _today(db)
        if after is not None:
            anchor = db.execute(
                "SELECT COALESCE(due_on,?) AS due, created_at, id FROM open_loops WHERE id=?",
                (UNDATED, after),
            ).fetchone()
            if anchor is None:
                raise LoopError("invalid_cursor", BAD_CURSOR, 422)
            where.append(f"({order}) > (?,?,?)")
            params.extend([anchor["due"], anchor["created_at"], anchor["id"]])
        rows = db.execute(
            f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    items = [_view(row, today) for row in rows[:limit]]
    return {
        "today": today,
        "timeZone": zone,
        "items": items,
        "nextAfter": items[-1]["id"] if len(rows) > limit else None,
    }


def redact(db, session_ids) -> None:
    """Forgetting: a chat's loops go with it (the caller owns the transaction)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='open_loops'").fetchone():
        return
    for session_id in session_ids:
        db.execute("DELETE FROM open_loops WHERE session_id=?", (session_id,))
