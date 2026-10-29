"""What a stop leaves inside a turn, settled from Pi's own records when Pi starts again.

Most of a turn is already durable: the input before any model call, each tool action before it is
dispatched, its result before the next model call. Two moments are not, and both are settled
here without running anything a second time:

- **An answer the model produced but Pi had not saved yet.** It is held here first and the final
  commit removes it in the same transaction, so a stop in between cannot drop it. At start the
  turn is completed with exactly that answer, through the same checks as a live commit: a stop
  the owner asked for, or a steer that arrived, still refuses it.
- **One of Conker's own tool calls (`conker.*`, see `pi.turns.native_tools`) cut off mid-way.**
  Its effect, a request or a question in the owner's Inbox, lives in Pi, so ToolGate cannot say
  whether it happened and asking it would leave the chat waiting forever. Pi's records decide:
  filed means the turn waits only for its reply; not filed means nothing happened, and the result
  says so plainly. The call itself is never run again.
"""

from __future__ import annotations

import json
import logging
import time

from pi.actions import identities as actions
from pi.actions.toolgate import ToolResult

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS held_answers (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id),
 text TEXT NOT NULL, fields TEXT NOT NULL, created_at REAL NOT NULL
);
"""

# Conker's own tools run inside Pi, so their tool ids carry this prefix (as `identity_scopes`).
OWN = "conker."
NOT_DONE = {
    "status": "not_done",
    "message": (
        "Pi restarted before this finished. Nothing was filed or saved. Tell the owner it was "
        "not done; they can ask again."
    ),
}
# Where a filed request stands, in the words the reply should use.
STATES = {
    "open": "is waiting in the owner's Inbox for their approval",
    "approved": "is done and listed in the owner's Inbox",
    "declined": "was declined by the owner",
    "failed": "failed; the owner's Inbox says why",
}


def hold(store, turn_id, text, fields):
    """Write the answer down before the final commit, which removes it again."""
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO held_answers VALUES(?,?,?,?)",
            (turn_id, text, json.dumps(fields, ensure_ascii=False), time.time()),
        )


def release(db, turn_id):
    """Inside the final-message transaction: the saved answer replaces the held one."""
    db.execute("DELETE FROM held_answers WHERE turn_id=?", (turn_id,))


def drop(store, turn_id):
    """The answer was refused (a steer or a stop); a restart must not bring it back."""
    with store._connect() as db:
        release(db, turn_id)


def recover(store) -> int:
    """At start, before running turns are marked interrupted: save each held answer."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT h.* FROM held_answers h JOIN turns t ON t.id=h.turn_id "
            "WHERE t.status='running' AND t.session_id NOT IN "
            "(SELECT session_id FROM forgotten_sessions)"
        ).fetchall()
    saved = 0
    for row in rows:
        try:
            store.complete_turn(row["turn_id"], row["text"], **json.loads(row["fields"]))
            saved += 1
        except Exception as exc:
            # Refused the way a live commit would be; the turn is then marked like any other.
            log.warning("A held answer was not saved after the restart (%s).", type(exc).__name__)
    with store._connect() as db:
        # Whatever is left belonged to a turn that already ended or was just refused.
        db.execute("DELETE FROM held_answers")
    return saved


def _effect(db, action):
    """What Conker's own call left in Pi: the request or question it filed, if any. Calls in a
    turn run one at a time, so anything this turn filed after the call began is its effect.
    Every one of Conker's own tools files one of these two; a tool with another effect must be
    added here, or a restart would report it as not done."""
    request = db.execute(
        "SELECT id,title,state FROM change_requests WHERE turn_id=? AND created_at>=? "
        "ORDER BY created_at LIMIT 1",
        (action["turn_id"], action["created_at"]),
    ).fetchone()
    if request is not None:
        title = request["title"].rstrip(".")
        where = STATES.get(request["state"], "is in the owner's Inbox")
        return {
            "status": "filed",
            "requestId": request["id"],
            "state": request["state"],
            "message": (
                f"Pi restarted during this call, after it was filed: “{title}” {where}. "
                "Tell them; do not file it again."
            ),
        }
    ask = db.execute(
        "SELECT id FROM owner_asks WHERE turn_id=? AND created_at>=? ORDER BY created_at LIMIT 1",
        (action["turn_id"], action["created_at"]),
    ).fetchone()
    if ask is not None:
        return {
            "status": "asked",
            "askId": ask["id"],
            "message": (
                "Pi restarted during this call, after the question was asked. It waits in the "
                "owner's Inbox; their answer arrives as their next message in this chat. Tell "
                "them in one line that you asked."
            ),
        }
    return None


def settle_own_tools(store) -> int:
    """At start: record how each of Conker's own calls cut off mid-way ended. Never re-runs one."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT a.* FROM tool_actions a JOIN turns t ON t.id=a.turn_id "
            "WHERE t.status='running' AND a.state='dispatching' AND substr(a.tool_id,1,?)=? "
            "AND t.session_id NOT IN (SELECT session_id FROM forgotten_sessions)",
            (len(OWN), OWN),
        ).fetchall()
        settled = [(dict(row), _effect(db, row)) for row in rows]
    for action, effect in settled:
        outcome = ToolResult(
            ok=effect is not None, result=effect or NOT_DONE, tool_id=action["tool_id"]
        )
        try:
            actions.record(store, action, outcome)
        except Exception as exc:
            # Left in flight: the turn is marked outcome_unknown, as before this check existed.
            log.warning("An interrupted Conker tool call was not settled (%s).", type(exc).__name__)
    return len(settled)


def redact(db, sessions):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='held_answers'").fetchone():
        for sid in sessions:
            db.execute(
                "DELETE FROM held_answers WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?)",
                (sid,),
            )
