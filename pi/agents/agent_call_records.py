"""The agent-call table, call-chain lookups and the run helpers (a pure move out of agent_calls.py
to keep that file under the size limit; the guards, privacy, forgetting and access reads stay
there)."""

from __future__ import annotations

import json

from pi.conversation import settings as session_settings
from pi.conversation import submissions

MAX_DEPTH = 2


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_calls (
 id TEXT PRIMARY KEY,
 parent_session_id TEXT NOT NULL REFERENCES sessions(id),
 parent_turn_id TEXT,
 child_session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
 agent_id TEXT NOT NULL,
 depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 2),
 parent_outside INTEGER NOT NULL CHECK(parent_outside IN (0,1)),
 state TEXT NOT NULL CHECK(state IN ('running','done','waiting','failed')),
 created_at REAL NOT NULL,
 request_id TEXT,
 reply_message_id TEXT
);
CREATE INDEX IF NOT EXISTS agent_calls_turn ON agent_calls(parent_turn_id);
CREATE INDEX IF NOT EXISTS agent_calls_recent ON agent_calls(created_at);
"""


def initialize(db):
    db.executescript(SCHEMA)


# The chat and the chats it continues, nearest first: an automatic fork's parent is closed as
# 'forked'; a message branch (`pi.conversation.forks`) leaves its source open and is recorded.
_LINEAGE = """
WITH RECURSIVE up(id, level) AS (
 SELECT ?, 0
 UNION ALL
 SELECT p.id, up.level + 1 FROM up JOIN sessions s ON s.id=up.id
 JOIN sessions p ON p.id=s.parent_id AND (p.status='forked' OR EXISTS (
  SELECT 1 FROM message_fork_requests b WHERE b.child_session=s.id AND b.source_session=p.id
 )) WHERE up.level < 1000
)
SELECT c.* FROM up JOIN agent_calls c ON c.child_session_id=up.id ORDER BY up.level LIMIT 1
"""


def _row(db, session_id):
    """The call that opened this chat, or the chat it continues; None for an ordinary chat."""
    if not session_id:
        return None
    return db.execute(_LINEAGE, (session_id,)).fetchone()


def _turn_row(db, turn_id):
    if not turn_id:
        return None
    turn = db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)).fetchone()
    return _row(db, turn["session_id"]) if turn else None


def depth(db, session_id) -> int:
    """0 for an ordinary chat; 1 or 2 for a chat an agent call opened."""
    row = _row(db, session_id)
    return row["depth"] if row else 0


def caller_turn(db, turn_id) -> str | None:
    """The turn whose call opened the chat this turn runs in; None for an ordinary chat."""
    row = _turn_row(db, turn_id)
    return row["parent_turn_id"] if row else None


def running_requests(store, turn_id) -> list[str]:
    """The submissions of the calls this turn started that may still be running."""
    if not turn_id:
        return []
    with store._connect() as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT request_id FROM agent_calls WHERE parent_turn_id=? AND state='running' "
                "AND request_id IS NOT NULL",
                (turn_id,),
            )
        ]


def _agent_of(db, session_id, turn_id):
    """The agent that ran a turn (its frozen settings), else the chat's selected agent."""
    row = db.execute("SELECT snapshot FROM turn_settings WHERE turn_id=?", (turn_id,)).fetchone()
    if row is None:
        row = db.execute(
            "SELECT settings AS snapshot FROM session_settings WHERE session_id=?", (session_id,)
        ).fetchone()
    try:
        return json.loads(row["snapshot"]).get("agentId") or "companion"
    except (TypeError, ValueError, AttributeError):
        return "companion"


def _chain(db, session_id, execution):
    """The caller's agent and every agent up the chain of calls that led to this chat."""
    found = {(execution or {}).get("agentId") or "companion"}
    current = session_id
    for _ in range(MAX_DEPTH + 1):
        row = _row(db, current)
        if row is None:
            break
        found.add(row["agent_id"])
        found.add(_agent_of(db, row["parent_session_id"], row["parent_turn_id"]))
        current = row["parent_session_id"]
    return found


def _finish(store, identity, state, reply_message_id=None):
    with store._connect() as db:
        db.execute(
            "UPDATE agent_calls SET state=?,reply_message_id=? WHERE id=?",
            (state, reply_message_id, identity),
        )
        db.commit()


def _stopped(store, turn_id) -> bool:
    """Whether the owner stopped this turn or any turn up its chain of calls."""
    with store._connect() as db:
        for _ in range(MAX_DEPTH + 1):
            if not turn_id:
                return False
            if db.execute(
                "SELECT 1 FROM turn_cancellations WHERE turn_id=?", (turn_id,)
            ).fetchone():
                return True
            turn_id = caller_turn(db, turn_id)
    return False


def _text(args, name, limit):
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Give {name} as text.")
    if len(value.strip()) > limit:
        raise ValueError(f"Give {name} in at most {limit} characters.")
    return value.strip()


def _content(message):
    content = (message or {}).get("content")
    if content is not None and not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return content


def _answer(store, request_id):
    """(turn id, turn status, reply message id, reply text) for the child turn, as Pi stored it."""
    try:
        row = submissions.get(store, request_id)
    except submissions.SubmissionError:
        return None, None, None, None
    if not row["turn_id"]:
        return None, row["status"], None, None
    final = row["final_message_id"]
    reply = _content(store.get_message(final) if final else None)
    return row["turn_id"], row["status"], final, reply


def _child_outside(store, outside, turn, fallback):
    if turn is None:
        return bool(fallback)
    try:
        # The chat the child's turn ran in: a fork of the child chat when its history was long.
        chat = (store.get_turn(turn) or {})["session_id"]
        return bool(outside(chat, turn, session_settings.execution(store, chat, turn)))
    except Exception:
        # Unreadable means outside, never an error to the model.
        return True


def _refusal(message):
    return False, {"status": "refused", "message": message, "outside": False}
