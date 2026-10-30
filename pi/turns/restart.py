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

The final commit can also fail while Pi keeps running: the database is locked, full or failing.
`save` tries it again a few times, a little later each time. If every try fails, the answer waits
here and a small settle (`settle`, every `SETTLE_SECONDS` while an answer waits) saves it once the
database works again, through the same checks, so the chat is not left `running` until the next
start. A stop or a steer that arrives meanwhile ends the turn as the startup settle would, and an
answer the database still refuses after `SETTLE_TRIES` tries ends its turn as failed, in plain
words. Nothing runs again: the commit only ever ends a turn that is still running.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

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

# Seconds between the tries of a final commit that hit a database error, while the turn waits on
# it (four tries; a locked database also waits out its busy timeout on each).
RETRY_DELAYS = (0.25, 1.0, 3.0)
# How often a waiting answer is tried again, and after how many failed tries its turn ends:
# about ten minutes, long enough for a held lock or a full disk to clear.
SETTLE_SECONDS = 15.0
SETTLE_TRIES = 40
NOT_SAVED = (
    "Pi could not save the answer yet. It is kept and appears in this chat once the database "
    "works again."
)
# How a turn ends when its waiting answer is not saved, by whether the turn had acted.
UNSAVED = {
    False: (
        "Pi could not save this answer: its database kept failing. Your message is kept; send it "
        "again to retry."
    ),
    True: (
        "The action ran, but Pi could not save the reply: its database kept failing. Ask for the "
        "reply; the action will not run again."
    ),
}
STEERED = {
    False: (
        "Your steer arrived before Pi could save the answer, so that answer was dropped. Send "
        "your message again to continue."
    ),
    True: (
        "The action ran. Your steer arrived before Pi could save the reply, so it was dropped. "
        "Ask for the reply; the action will not run again."
    ),
}


class NotSaved(Exception):
    """Every try at the final commit failed in the database; the answer now waits for `settle`."""


@dataclass
class _Waiting:
    store: object
    turn_id: str
    text: str
    fields: dict
    held: bool
    tries: int = 0


_waiting: dict[str, _Waiting] = {}
_lock = threading.Lock()
_settler: threading.Thread | None = None
_pause = time.sleep  # The wait between the tries of a live commit.


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


def end(store, turn_id, detail):
    """End a running turn whose answer was refused or not saved, and drop that answer, in one
    commit. `detail` is the words, or one entry per whether the turn had acted. A turn the owner
    stopped ends `cancelled`, as every failed turn does. Returns whether the turn had acted."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT acted FROM turns WHERE id=?", (turn_id,)).fetchone()
        acted = bool(row and row["acted"])
        store._finish_turn(
            db,
            turn_id,
            "acted_no_reply" if acted else "failed",
            acted=int(acted),
            detail=detail[acted] if isinstance(detail, dict) else detail,
        )
        release(db, turn_id)
        db.commit()
    return acted


def save(store, turn_id, text, fields):
    """The final commit while Pi runs: hold the answer, then save it as the turn's end.

    A database error is tried again after each of RETRY_DELAYS. An error the database raised on
    purpose (a constraint, a damaged file) is not, as the same try would fail the same way. When
    the tries are spent the answer waits for `settle` and this raises NotSaved. A refusal (an
    owner stop, a steer, a turn no longer running) raises at once, as it always did.
    """
    from pi.models.providers import ProviderUnavailable

    held = False
    for attempt, delay in enumerate((*RETRY_DELAYS, None)):
        try:
            if not held:
                hold(store, turn_id, text, fields)
                held = True
            return store.complete_turn(turn_id, text, **fields)
        except sqlite3.OperationalError as exc:
            failure = exc
        except sqlite3.DatabaseError as exc:
            failure = exc
            break
        except ProviderUnavailable:
            raise
        except RuntimeError:
            # A try that committed and lost only its answer back: the saved answer stands.
            saved = _saved(store, turn_id) if attempt else None
            if saved is None:
                raise
            return saved
        if delay is not None:
            _pause(delay)
    _keep(_Waiting(store, turn_id, text, dict(fields), held))
    log.warning("An answer was not saved (%s); it waits for the database.", type(failure).__name__)
    raise NotSaved(NOT_SAVED) from failure


def _saved(store, turn_id):
    with store._connect() as db:
        row = db.execute(
            "SELECT tm.message_id FROM turn_messages tm JOIN turns t ON t.id=tm.turn_id "
            "WHERE tm.turn_id=? AND tm.purpose='final' AND t.status='complete'",
            (turn_id,),
        ).fetchone()
    return store.get_message(row[0]) if row else None


def waiting() -> list[str]:
    """The turns whose answer waits for the database to work again."""
    with _lock:
        return list(_waiting)


def settle() -> int:
    """Try each waiting answer once more. Returns how many turns it settled (saved or ended)."""
    with _lock:
        entries = list(_waiting.values())
    settled = 0
    for entry in entries:
        if _settle(entry):
            with _lock:
                _waiting.pop(entry.turn_id, None)
            settled += 1
    return settled


def _settle(entry) -> bool:
    """One more try, through every check of a live commit. True when the turn is settled: saved,
    or ended the way the startup settle would end it."""
    from pi.models.providers import ProviderUnavailable
    from pi.turns import steering

    store, turn_id = entry.store, entry.turn_id
    try:
        if not entry.held:
            hold(store, turn_id, entry.text, entry.fields)
            entry.held = True
        store.complete_turn(turn_id, entry.text, **entry.fields)
        log.info("A waiting answer was saved.")
        return True
    except sqlite3.OperationalError as exc:
        entry.tries += 1
        if entry.tries < SETTLE_TRIES:
            return False
        detail, failure = UNSAVED, exc
    except sqlite3.DatabaseError as exc:
        detail, failure = UNSAVED, exc
    except steering.Pending as exc:
        # Live, the turn would answer again with the steer; nothing here calls a model.
        detail, failure = STEERED, exc
    except ProviderUnavailable as exc:
        # An owner stop (the turn ends cancelled) or a call that ended.
        detail, failure = exc.reason, exc
    except RuntimeError:
        # The turn ended another way, or this store closed (Pi stopping; the next start settles).
        with contextlib.suppress(Exception):
            drop(store, turn_id)
        return True
    except Exception as exc:
        detail, failure = UNSAVED, exc
    try:
        end(store, turn_id, detail)
    except Exception:
        return False  # Still failing: the next round tries the save again first.
    log.warning("A waiting answer was not saved (%s); its turn ended.", type(failure).__name__)
    return True


def _keep(entry):
    global _settler
    with _lock:
        _waiting[entry.turn_id] = entry
        if _settler is None or not _settler.is_alive():
            _settler = _start()


def _start():
    thread = threading.Thread(target=_run, name="pi-answer-settle", daemon=True)
    thread.start()
    return thread


def _run():
    """The settle's own thread, alive only while an answer waits."""
    global _settler
    while True:
        time.sleep(SETTLE_SECONDS)
        try:
            settle()
        except Exception as exc:
            log.warning("Waiting answers were not settled this round (%s).", type(exc).__name__)
        with _lock:
            if not _waiting:
                if _settler is threading.current_thread():
                    _settler = None
                return


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
