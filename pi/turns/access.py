"""Access for one action, from the owner's own settings: chat > agent > tool, three guards.

Each action resolves to allow, ai (Pi's reviewer decides; unsure means ask) or ask (the owner, in
the Inbox). The layers: the chat's switch (`settings.access`), the agent's `toolAccess`, the tool's
own default (ToolGate `authorization`). The guards: a tool the agent cannot see does not exist for
it (ToolGate scope and the agent's toolkit); the floor (sensitive tools) always asks; a run that has
read outside content cannot use a loosening below the tool's own default one step further. His own
typed message counts as approval when it asks for exactly this action. The reviewer
(`pi.turns.reviewer`) checks both; in AI decides a tool whose Inbox card he set to send, publish,
ship, delete or pay always asks him, and the reviewer's own "reaches a person" or "can't be undone"
is one more reason to ask, never a reason to run. ToolGate enforces the floor again and accepts
"run" only from a key he made a policy delegate. See context/design/automation-and-access.md.

Outside content is any text in the turn the owner did not write: a result from a tool that is not
Conker's own, a webhook payload (once one arrives, for the whole chat, `mark_outside`), memories
recalled for this turn, an attachment or a project source, or his answer to Conker's question
(the question and its options were written by the AI).
"""

from __future__ import annotations

import json
import time
import uuid

from pi.agents import asks
from pi.memory import store as memory_store
from pi.schedule import agent_jobs
from pi.turns import reviewer

LEVELS = ("allow", "ai", "ask")
RANK = {"allow": 0, "ai": 1, "ask": 2}
STRICTER = {"allow": "ai", "ai": "ask", "ask": "ask"}
TOOL_DEFAULT = {"auto": "allow", "ai_review": "ai", "owner_confirmation": "ask"}
CHAT_LEVELS = ("default", "ask", "ai", "full")
# Wrapped by Pi around a webhook's payload; a run that carries one has read outside content.
WEBHOOK_MARK = "<webhook>"
# Conker's question to him (`pi.turns.native_tools.ASK`) and a tool draft (`DRAFT_TOOL`).
native_ask = "conker.ask"
native_draft = "conker.draft_tool"
native_schedule = "conker.schedule"

SCHEMA = """
CREATE TABLE IF NOT EXISTS handled_actions (
 id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id), turn_id TEXT, action_id TEXT,
 tool_id TEXT NOT NULL, decided_by TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS handled_actions_recent ON handled_actions(created_at);
"""


def initialize(db):
    db.executescript(SCHEMA)


def resolve(*, authorization, sensitive, agent_level, chat, outside):
    """(level, decided_by) for one action. Pure: every input is owner-set or observed by Pi."""
    if authorization == "blocked":
        return "blocked", "tool_default"
    default = TOOL_DEFAULT.get(authorization, "ask")
    level, by = default, "tool_default"
    if agent_level in LEVELS:
        level, by = agent_level, "agent"
    if chat == "ask":
        # Ask me: no loosening below the tool's default, and nothing left to the AI.
        if RANK[level] < RANK[default]:
            level, by = default, "chat"
        if level == "ai":
            level, by = "ask", "chat"
    elif chat == "ai" and level == "ask":
        level, by = "ai", "chat"
    elif chat == "full" and level != "allow":
        level, by = "allow", "chat"
    if outside and RANK[level] < RANK[default]:
        # Someone else's text cannot use his loosening: one step back toward the tool's default.
        level = STRICTER[level]
    if sensitive:
        level, by = "ask", "tool_default"
    return level, by


def mark_outside(store, session_id, reason="webhook"):
    """From now on this chat holds someone else's text: every turn in it (and in its forks) counts
    as having read outside content, and none of its messages is sent to memory."""
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO outside_sessions VALUES (?,?,?)",
            (session_id, reason, time.time()),
        )


# The chat's own messages and those it inherited from another chat (a reviewed fork).
_IN_CHAT = (
    "(m.session_id=? OR m.id IN "
    "(SELECT message_id FROM context_inherited_messages WHERE session_id=?))"
)


def outside_content(store, session_id, owner_text, native, *, turn_id=None, execution=None):
    """True once this turn can read text the owner did not write (see the module notes)."""
    if WEBHOOK_MARK in (owner_text or "") or (execution or {}).get("projectContext"):
        return True
    with store._connect() as db:
        if turn_id and asks.answer_words(db, turn_id) is not None:
            return True
        chat = (session_id, session_id)
        if db.execute(
            "SELECT 1 FROM outside_sessions WHERE session_id=? OR session_id IN (SELECT "
            "m.session_id FROM messages m JOIN context_inherited_messages i ON i.message_id=m.id "
            "WHERE i.session_id=?)",
            chat,
        ).fetchone():
            return True
        if db.execute(
            f"SELECT 1 FROM messages m WHERE {_IN_CHAT} AND m.role='user' AND m.content LIKE ?",
            (*chat, f"%{WEBHOOK_MARK}%"),
        ).fetchone():
            return True
        if db.execute(
            "SELECT 1 FROM message_attachments a JOIN messages m ON m.id=a.message_id "
            f"WHERE {_IN_CHAT}",
            chat,
        ).fetchone():
            return True
        rows = db.execute(
            f"SELECT m.content FROM messages m WHERE {_IN_CHAT} AND m.role='tool'", chat
        ).fetchall()
    if turn_id and memory_store.recalled(memory_store.context(store, turn_id)["package"]):
        return True
    for row in rows:
        try:
            # Stored as the JSON of the observation text, which is itself JSON.
            observation = json.loads(_text(row["content"]))
            tool = observation.get("tool")
        except (ValueError, AttributeError):
            return True
        # Pi's own note that he said no (beside the tool id, where tool output cannot reach).
        if "declined" in observation and "result" not in observation:
            continue
        if not native(str(tool)):
            return True
    return False


def _text(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    return value if isinstance(value, str) else raw


def owner_text(store, turn_id):
    """The owner's own message that started this turn (a cron job's instructions in a job run).

    For his answer to one of Conker's questions, only the words he typed: an option he tapped
    was written by the AI, so it is no request of his. In a run of the owner's own job, only the
    job's saved instructions: Pi wrote the rest of that message (the preamble, the run's date and
    time, why it started). Nothing in a run of a job Conker made on its own: the model wrote those
    instructions, and no owner approved them."""
    with store._connect() as db:
        words = asks.answer_words(db, turn_id)
        if words is not None:
            return words
        run = agent_jobs.run_of(db, turn_id)
        if run is not None:
            return run.get("instructions", "") if run.get("createdBy", "owner") == "owner" else ""
        row = db.execute(
            "SELECT m.content FROM turn_messages t JOIN messages m ON m.id=t.message_id "
            "WHERE t.turn_id=? AND t.purpose='input' AND m.role='user'",
            (turn_id,),
        ).fetchone()
    return _text(row["content"]) if row else ""


def record(store, *, session_id, turn_id, action_id, tool_id, decided_by, reason, now=None):
    """One action that ran without asking him, for Inbox Handled."""
    with store._connect() as db:
        db.execute(
            "INSERT INTO handled_actions VALUES (?,?,?,?,?,?,?,?)",
            (
                "hnd_" + uuid.uuid4().hex,
                session_id,
                turn_id,
                action_id,
                tool_id,
                decided_by,
                reason[:500],
                time.time() if now is None else now,
            ),
        )
        db.commit()


def handled(store, since):
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM handled_actions WHERE created_at>=? ORDER BY created_at DESC LIMIT 200",
            (since,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "sessionId": row["session_id"],
            "turnId": row["turn_id"],
            "toolId": row["tool_id"],
            "decidedBy": row["decided_by"],
            "reason": row["reason"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def redact(db, session_ids):
    """Forget with the chat. An older database may not have the table yet."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='handled_actions'"
    ).fetchone():
        return
    for identity in session_ids:
        db.execute("DELETE FROM handled_actions WHERE session_id=?", (identity,))


def decide(loop, tool, args, *, execution, session_id, turn_id, native, delegate):
    """("run" | "ask" | None, decided_by, reason) for one action. None: let the tool's own
    authorization apply (blocked, or a ToolGate tool while Pi's key is not a policy delegate, so
    nothing Pi decides would be honoured; Conker's own tools are always Pi's to decide)."""
    if tool.id == native_ask:
        # Asking him is the safe path: it changes nothing and his answer comes back as his words.
        return "run", "tool_default", REASONS["tool_default"]
    if tool.id == native_draft:
        # A draft only puts a card in his Inbox; nothing exists until he publishes it, so there is
        # nothing to review now, and the chat does not wait for a reviewer's model call.
        return "ask", "tool_default", ""
    if not native(tool.id) and not delegate():
        return None, "tool_default", ""
    settings = (execution or {}).get("settings") or {}
    chat = settings.get("access", "default")
    agent_level = (((execution or {}).get("configuration") or {}).get("toolAccess") or {}).get(
        tool.id
    )
    request = owner_text(loop.store, turn_id)
    outside = outside_content(
        loop.store, session_id, request, native, turn_id=turn_id, execution=execution
    )
    level, by = resolve(
        authorization=tool.authorization,
        sensitive=tool.sensitive,
        agent_level=agent_level,
        chat=chat,
        outside=outside,
    )
    if level == "blocked":
        return None, by, ""
    if level == "allow" and outside and native(tool.id):
        # Keep after outside content (a memory, a cron job): someone else's text could be planting
        # it, so the reviewer checks it against what he asked.
        level = "ai"
    if level == "allow":
        return "run", by, REASONS[by]
    if level == "ai":
        approved, why = reviewer.review(loop, execution, "ai", request, tool, args, outside=outside)
        return ("run", "ai_reviewer", why) if approved else ("ask", by, why)
    if tool.id == native_schedule:
        # A recurring job made from his words would still run instructions the model wrote:
        # unless his settings loosened it, it waits for his approval like a tool draft.
        return "ask", by, ""
    if request.strip() and not outside and not tool.sensitive:
        approved, why = reviewer.review(loop, execution, "request", request, tool, args)
        if approved:
            return "run", "owner_request", why
    return "ask", by, ""


def loosened(tool, decided_by, native):
    """Whether this run went ahead without him where the tool alone would not have: Handled. A
    question to him is not: it waits in his Inbox."""
    if tool.id == native_ask:
        return False
    return native or (
        TOOL_DEFAULT.get(tool.authorization) != "allow" and decided_by != "tool_default"
    )


REASONS = {
    "tool_default": "Allowed by the tool's own setting.",
    "agent": "Allowed by this agent's setting for the tool.",
    "chat": "Allowed by this chat's access switch.",
}
