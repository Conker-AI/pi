"""One agent runs another (`conker.run_agent`): instructions in a chat of its own, its reply back.

The caller writes instructions; Pi opens a new chat with the target agent (a child of the caller's
chat, so forgetting the caller's chat forgets it too, and its history starts empty), runs one turn
there and hands the reply back to the caller. The child runs with the target agent's own
owner-set toolkit and access and always with memory off: its one "user" message was written by a
model and must never reach memory as the owner's. Of the caller chat's access switch only "ask"
carries over; a loosening (AI or Full) was his for that chat and that agent, never for another
agent's wider toolkit, so the child starts at "default".

The caller's chat keeps only a pointer to the reply (`callId`); the model reads the reply itself,
from the child chat, each time the history is built (`expand`). Forgetting or trashing the child
chat therefore takes the reply out of what the caller's model sees too. A Stop on the caller's turn
reaches every call it started: `pi.turns.control` stops the running child turns, and every guard
in a child turn checks the turns up its chain (`caller_turn`). A child that acted marks the caller's
turn acted. A continuation of a child chat (a fork, automatic or his) is still that call's chat.

Guards, each checked again when the chat is opened, in one write transaction:
- the target exists, is not archived, and is neither the caller nor any agent up the call chain;
- depth at most 2 (companion -> A -> B); a depth-2 chat is not offered the tool at all;
- at most 3 calls per turn and 30 in any 24 hours, in all;
- never from a team role's turn (its budget does not meter a child) or a run of an AI job
  (`pi.turns.native_tools`).

`pi.turns.access` reads this table: no turn in a child chat has an owner request (`owner_text` is
empty, so his words never approve anything there), and a child chat whose caller had read outside
content counts as outside for every turn. Every result carries `outside` explicitly; the caller
counts the reply as outside unless it is False. This module never imports the turn loop or
`pi.turns`: the loop is passed in, and the outside check is a callback.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid

from pi.agents import profiles
from pi.conversation import settings as session_settings
from pi.conversation import submissions

MAX_DEPTH = 2
PER_TURN = 3
PER_DAY = 30
DAY = 24 * 60 * 60
MAX_INSTRUCTIONS = 8000
MAX_REPLY = 8000
RUN_AGENT_ID = "conker.run_agent"

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


class Refused(Exception):
    """A call this turn may not make, in words the model can pass on."""


def initialize(db):
    db.executescript(SCHEMA)


# The chat and the chats it continues (each fork's parent is closed as 'forked'), nearest first.
_LINEAGE = """
WITH RECURSIVE up(id, level) AS (
 SELECT ?, 0
 UNION ALL
 SELECT p.id, up.level + 1 FROM up JOIN sessions s ON s.id=up.id
 JOIN sessions p ON p.id=s.parent_id AND p.status='forked' WHERE up.level < 1000
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


def called(db, turn_id) -> bool:
    """Whether this turn runs in a chat an agent call opened (its input was written by a model)."""
    return _turn_row(db, turn_id) is not None


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


def parent_outside(db, session_id) -> bool:
    """Whether the turn that opened this chat had read text the owner did not write."""
    row = _row(db, session_id)
    return bool(row and row["parent_outside"])


def _refused(db, session_id, turn_id, now):
    if depth(db, session_id) >= MAX_DEPTH:
        return (
            "This agent was itself started by another agent two levels down, so it cannot start "
            "one more. Answer with what you have."
        )
    in_turn = db.execute(
        "SELECT COUNT(*) FROM agent_calls WHERE parent_turn_id=?", (turn_id,)
    ).fetchone()[0]
    if turn_id and in_turn >= PER_TURN:
        return (
            f"You already ran {PER_TURN} agents in this turn, the most one turn may. Answer with "
            "what you have."
        )
    today = db.execute(
        "SELECT COUNT(*) FROM agent_calls WHERE created_at>?", (now - DAY,)
    ).fetchone()[0]
    if today >= PER_DAY:
        return (
            f"Agents were run {PER_DAY} times in the last 24 hours, the most allowed. Tell the "
            "owner; they can talk to that agent in a chat of its own."
        )
    return None


def refused(store, session_id, turn_id, now=None) -> str | None:
    """Why this turn may not run another agent; None if it may."""
    with store._connect() as db:
        return _refused(db, session_id, turn_id, time.time() if now is None else now)


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


def _target(db, wanted):
    row = db.execute(
        "SELECT id FROM agents WHERE id=? OR name_key=?", (wanted, wanted.casefold())
    ).fetchone()
    if row is None:
        raise Refused(f"There is no agent called “{wanted}”. Use an agent's exact name.")
    agent = profiles._get(db, row["id"])
    if agent["archived_at"] is not None:
        raise Refused(
            f"{agent['configuration']['name']} is archived. Only the owner can restore it."
        )
    return agent


def _privacy(db, parent, execution):
    live = session_settings.source_privacy(db, parent)
    if live is None:
        raise Refused("This chat is no longer available, so no agent can be started from it.")
    frozen = (execution or {}).get("privacy") or {}
    return {
        # Always off: the child's one message was written by a model, never by the owner.
        "memoryDisabled": True,
        "harnessDisabled": bool(live["harnessDisabled"] or frozen.get("harnessDisabled")),
    }


def _access(execution):
    """Only "ask" carries over to the child; any loosening was for the caller's own chat."""
    chosen = ((execution or {}).get("settings") or {}).get("access", "default")
    return "ask" if chosen == "ask" else "default"


def _open(store, wanted, *, session_id, turn_id, execution, outside, request_id):
    """Check every guard and open the child chat, in one write transaction."""
    now = time.time()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        why = _refused(db, session_id, turn_id, now)
        if why:
            raise Refused(why)
        agent = _target(db, wanted)
        name = agent["configuration"]["name"]
        if agent["id"] in _chain(db, session_id, execution):
            raise Refused(
                f"{name} is already working on this (it is you, or it started this chain of "
                "agents), so it cannot be run again here. Do the work yourself."
            )
        level = depth(db, session_id) + 1
        if level > MAX_DEPTH:
            raise Refused(
                "Agents can start agents only two levels deep. Answer with what you have."
            )
        settings = session_settings.Settings.model_validate(
            {
                "agentId": agent["id"],
                "privacy": _privacy(db, session_id, execution),
                "access": _access(execution),
            }
        )
        child = "ses_" + uuid.uuid4().hex[:16]
        # The name only: the instructions are the caller's words and stay in its chat.
        title = f"Agent call: {name}"
        db.execute(
            "INSERT INTO sessions(id,parent_id,title,status,created_at) VALUES(?,?,?,'open',?)",
            (child, session_id, title, now),
        )
        # Raises if the caller's chat carries context whose privacy is unknown.
        session_settings.inherited_privacy(db, child)
        db.execute(
            "INSERT INTO session_settings VALUES (?,1,?) ON CONFLICT(session_id) "
            "DO UPDATE SET revision=1,settings=excluded.settings",
            (child, settings.model_dump_json()),
        )
        identity = "acall_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO agent_calls(id,parent_session_id,parent_turn_id,child_session_id,"
            "agent_id,depth,parent_outside,state,created_at,request_id) "
            "VALUES (?,?,?,?,?,?,?,'running',?,?)",
            (
                identity,
                session_id,
                turn_id,
                child,
                agent["id"],
                level,
                int(outside),
                now,
                request_id,
            ),
        )
        db.commit()
    return identity, child, agent


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


def _child_outside(store, outside, child, turn, fallback):
    if turn is None:
        return bool(fallback)
    try:
        return bool(outside(child, turn, session_settings.execution(store, child, turn)))
    except Exception:
        # Unreadable means outside, never an error to the model.
        return True


def _refusal(message):
    return False, {"status": "refused", "message": message, "outside": False}


def run(loop, args, *, session_id, turn_id, execution, run, outside):
    """(ok, result) for one call. `run` is False when the owner's access layers ask first.
    `outside(session_id, turn_id, execution)` says whether a turn read outside content."""
    store = loop.store
    why = refused(store, session_id, turn_id)
    if why:
        return _refusal(why)
    try:
        wanted = _text(args, "agent", 200)
        instructions = _text(args, "instructions", MAX_INSTRUCTIONS)
    except ValueError as invalid:
        return False, {"status": "invalid", "message": str(invalid), "outside": False}
    if not run:
        return _refusal(
            "The owner's settings ask first before another agent is run here, and that cannot "
            "wait in the Inbox. Use conker.ask to ask whether to run it, or do the work yourself."
        )
    try:
        parent_out = bool(outside(session_id, turn_id, execution))
    except Exception:
        # Unreadable means outside.
        parent_out = True
    request_id = "agentcall_" + uuid.uuid4().hex
    try:
        identity, child, agent = _open(
            store,
            wanted,
            session_id=session_id,
            turn_id=turn_id,
            execution=execution,
            outside=parent_out,
            request_id=request_id,
        )
    except Refused as no:
        return _refusal(str(no))
    except profiles.AgentError:
        return _refusal("No agent can be started from this chat. Answer with what you have.")
    name = agent["configuration"]["name"]
    if _stopped(store, turn_id):
        _finish(store, identity, "failed")
        return _refusal("The owner stopped this turn, so no agent was run.")
    # A child's failure is an answer to the caller, never a crash: what Pi stored says how it went.
    with contextlib.suppress(Exception):
        loop.run_turn(child, instructions, request_id=request_id)
    turn, status, final, reply = _answer(store, request_id)
    if turn and turn_id and (store.get_turn(turn) or {}).get("acted"):
        # What the child did, the caller's turn did.
        store.mark_acted(turn_id)
    child_out = _child_outside(store, outside, child, turn, parent_out)
    base = {
        "callId": identity,
        "agentId": agent["id"],
        "agentName": name,
        "sessionId": child,
        "outside": child_out,
    }
    if status == "complete" and reply is not None:
        _finish(store, identity, "done", final)
        return True, {"status": "done", **base, "message": _read(name)}
    waiting = status in ("awaiting_approval", "awaiting_budget")
    _finish(store, identity, "waiting" if waiting else "failed")
    return False, {
        "status": "failed",
        **base,
        "message": (
            f"{name} is waiting for the owner's approval in its own chat, so there is no reply "
            "yet. Tell the owner it waits there."
            if waiting
            else f"{name} could not finish. Tell the owner, or answer without it."
        ),
    }


def _read(name):
    return (
        f"This is {name}'s reply, written by an AI agent, not by the owner: check it before you "
        "rely on it. Its chat is in the owner's Chats."
    )


GONE = (
    "That agent's chat was forgotten or moved to Trash, so its reply is no longer available. "
    "Do not repeat what it said."
)


# How far up from the reply's chat to look for the agent's chat (forks of forks during its turn).
_CHAIN_LIMIT = 32


def _owner_hid(db, child, reply_chat) -> bool:
    """Whether the owner forgot or trashed the agent's chat, the continuation holding its reply
    (a fork made during its turn) or a chat between them. Not `visibility.hidden`: that also hides
    every chat below a hidden one, and the agent's chat sits below the caller, whose own history
    still reads the reply while the caller is in the Trash and opened by its own id. Every other
    chat that carries the caller's lines sits below the caller too, so it is hidden as a whole."""
    chain, current = [reply_chat], reply_chat
    for _ in range(_CHAIN_LIMIT):
        if current == child:
            break
        parent = db.execute("SELECT parent_id FROM sessions WHERE id=?", (current,)).fetchone()
        if parent is None or not parent[0]:
            chain = [reply_chat, child]
            break
        current = parent[0]
        chain.append(current)
    else:
        chain = [reply_chat, child]
    ids = sorted(set(chain))
    marks = ",".join("?" * len(ids))
    # The chats the owner marked himself, as in visibility's sources before their descendants.
    marked = db.execute(
        f"SELECT 1 FROM sessions WHERE id IN ({marks}) AND status='forgotten' "
        f"UNION ALL SELECT 1 FROM session_trash WHERE session_id IN ({marks}) "
        f"UNION ALL SELECT 1 FROM forgotten_sessions WHERE session_id IN ({marks}) LIMIT 1",
        ids * 3,
    ).fetchone()
    return marked is not None


def expand(store, role, text):
    """The model's view of one history message: a call's record gets the reply back from the
    child chat, unless that chat was forgotten or trashed since."""
    if role != "tool" or RUN_AGENT_ID not in text:
        return text
    try:
        observation = json.loads(text)
    except ValueError:
        return text
    if not isinstance(observation, dict) or observation.get("tool") != RUN_AGENT_ID:
        return text
    result = observation.get("result")
    identity = result.get("callId") if isinstance(result, dict) else None
    if not isinstance(identity, str) or result.get("status") != "done":
        return text
    with store._connect() as db:
        row = db.execute(
            "SELECT c.reply_message_id, c.child_session_id, m.session_id FROM agent_calls c "
            "JOIN messages m ON m.id=c.reply_message_id WHERE c.id=?",
            (identity,),
        ).fetchone()
        shown_reply = row is not None and not _owner_hid(db, row[1], row[2])
    reply = _content(store.get_message(row[0])) if shown_reply and row[0] else None
    if reply is None:
        shown = {**result, "status": "gone", "message": GONE}
    else:
        shown = {
            **result,
            "reply": reply[:MAX_REPLY],
            "truncated": len(reply) > MAX_REPLY,
            "message": _read(result.get("agentName") or "the agent"),
        }
    return json.dumps({"tool": RUN_AGENT_ID, "result": shown}, ensure_ascii=False)


def redact(db, session_ids):
    """Forget with the chat, as caller or as the chat a call opened. An older database may not
    have the table yet."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_calls'"
    ).fetchone():
        return
    for identity in session_ids:
        db.execute(
            "DELETE FROM agent_calls WHERE parent_session_id=? OR child_session_id=?",
            (identity, identity),
        )
