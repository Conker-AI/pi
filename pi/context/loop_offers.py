"""Offering an open loop from the owner's own words (R-M7, L2).

The proposal pass (`pi.schedule.proposals`) finds a promise in a message the owner wrote. Here it
becomes a `promise` change request in his Inbox, the same card `conker.promise` uses after
outside content. Nothing is saved until he approves it (`open_loops.approve_request`). Rules:

- the cited message is a user message in a chat he can see; hidden, forgotten, private-memory
  and job chats are not offered (the pass does not read them, and this checks again at write time,
  inside the pass's own transaction);
- chats whose "user" text Pi or a model wrote (team roles, agent calls) are never offered;
- one offer per message, ever: a declined or approved one is remembered by its message id;
- the chat's and the Inbox's open-request caps apply; a full Inbox skips the offer quietly;
- a due date that is not a real day, or is already past, is dropped; the offer stays, undated.
"""

from __future__ import annotations

from pi.agents import change_requests
from pi.context import open_loops
from pi.conversation import visibility


def _today(db) -> str:
    return open_loops._today(db)[0]


def _owner_wrote(db, session_id: str, message_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM messages m JOIN sessions s ON s.id=m.session_id "
        "WHERE m.id=? AND m.session_id=? AND m.role='user' AND s.status<>'forgotten'",
        (message_id, session_id),
    ).fetchone()
    if row is None or visibility.hidden(db, [session_id]):
        return False
    for table, column in (
        ("agent_job_sessions", "session_id"),
        ("team_steps", "session_id"),
        ("agent_calls", "child_session_id"),
    ):
        if db.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (session_id,)).fetchone():
            return False
    privacy = db.execute(
        "SELECT 1 FROM message_privacy WHERE message_id=? AND (memory_disabled=1 "
        "OR harness_disabled=1)",
        (message_id,),
    ).fetchone()
    return privacy is None


def _already_offered(db, message_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM change_requests WHERE kind='promise' "
            "AND json_extract(payload,'$.messageId')=?",
            (message_id,),
        ).fetchone()
        is not None
    )


def _room(db, session_id: str) -> bool:
    per_chat = db.execute(
        "SELECT COUNT(*) FROM change_requests WHERE state='open' AND session_id=?", (session_id,)
    ).fetchone()[0]
    overall = db.execute(
        f"SELECT COUNT(*) FROM change_requests WHERE state='open' AND {change_requests.VISIBLE}"
    ).fetchone()[0]
    return per_chat < change_requests.PER_CHAT and overall < change_requests.OPEN_LIMIT


def _due(db, value: str | None) -> str | None:
    try:
        due = open_loops.due_date(value)
    except open_loops.LoopError:
        return None
    return due if due is not None and due >= _today(db) else None


def offer(db, candidates: list[dict], now: float) -> int:
    """File the candidates as open `promise` requests inside the caller's transaction; how many
    were filed. A candidate that breaks a rule above is skipped, never an error."""
    filed = 0
    for item in candidates:
        session_id, message_id = item["session_id"], item["message_id"]
        if not _owner_wrote(db, session_id, message_id):
            continue
        if _already_offered(db, message_id) or not _room(db, session_id):
            continue
        try:
            text = open_loops._clean(item["text"])
        except open_loops.LoopError:
            continue
        due = _due(db, item.get("due_on"))
        who_when = f", due {due}" if due else ""
        change_requests.file_in(
            db,
            "promise",
            f"Track: {text[:120]}",
            f"You said{who_when}: {text}"[:600],
            {"text": text, "by": "owner", "dueOn": due, "messageId": message_id},
            session_id=session_id,
            now=now,
        )
        filed += 1
    return filed
