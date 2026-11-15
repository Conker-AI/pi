"""`conker.promise`: Conker puts one promise on the owner's open-loops list.

The promise is the owner's ("I'll send it Friday") or Conker's own ("I'll check this tomorrow").
It is filed as a `promise` change request (`pi.agents.change_requests`) and made real by the same
access layers as `conker.remember`: at once where his settings allow (the loop is listed in Inbox
Handled), after outside content only by his tap in the Inbox (`pi.turns.access.decide`). A private
chat is never offered it (`pi.turns.native_tools.PRIVATE_REFUSED`): nothing said there may reach
a list that every other chat and the Morning check-in read.

The model never sees message ids, so the loop cites the turn's own input message, and only when
the owner wrote it (not Pi's preamble of a job run or a team role). It has no clock either, so the
tool says today's date, and a due date already past is refused with that date.

Design: context/design/open-loops.md in the Conker workspace.
"""

from __future__ import annotations

from dataclasses import replace

from pi.actions.toolgate import Tool
from pi.agents import change_requests
from pi.context import open_loops
from pi.turns import access

PROMISE = "conker.promise"
TOOL = Tool(
    PROMISE,
    "Track a promise",
    "Put one promise on the owner's open-loops list: something they said they would do, or "
    'something you just promised to do ("I\'ll check this tomorrow"). Do it on your own as soon '
    "as it is said; never ask whether to track it. Only real commitments, not wishes or small "
    "talk. The list is in their Morning check-in and `conker loops`, where they close it. "
    "Depending on the owner's settings it is saved at once or waits for their approval; the "
    "result says which.",
    [
        {
            "name": "text",
            "type": "string",
            "required": True,
            "description": "The promise as a short full sentence, e.g. Send Maya the invoice",
        },
        {
            "name": "by",
            "type": "string",
            "required": False,
            "description": "owner (they promised; the default) or conker (you promised)",
        },
        {
            "name": "due_on",
            "type": "string",
            "required": False,
            "description": "YYYY-MM-DD, only when the words fix a day; leave out otherwise",
        },
    ],
    "auto",
)


def dated(store, tool: Tool) -> Tool:
    """The tool with today's date (the owner's day) in its description."""
    try:
        day = open_loops.today(store)
    except Exception:  # an unreadable preference costs only the hint; a due date is still checked
        return tool
    return replace(tool, description=f"{tool.description} Today is {day} (the owner's day).")


def file(loop, args, *, session_id, turn_id) -> dict:
    """File the promise as a change request. ValueError: a bad argument, in words for the model."""
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Give text as text.")
    text = text.strip()[: open_loops.MAX_TEXT]
    by = str(args.get("by") or "owner").strip().lower()
    if by not in ("owner", "conker"):
        raise ValueError("Give by as owner or conker.")
    due = args.get("due_on")
    if due is not None and not isinstance(due, str):
        raise ValueError("Give due_on as YYYY-MM-DD.")
    try:
        due = open_loops.due_date(due.strip() or None) if due else None
    except open_loops.LoopError as invalid:
        raise ValueError(invalid.message) from None
    today = open_loops.today(loop.store)
    if due is not None and due < today:
        raise ValueError(f"{due} is already past; today is {today}. Give a day from today on.")
    payload = {
        "text": text,
        "by": by,
        "dueOn": due,
        "messageId": _input_message(loop.store, session_id, turn_id),
    }
    who = "The owner" if by == "owner" else "Conker"
    when = f", due {due}" if due else ""
    return change_requests.file(
        loop.store,
        "promise",
        f"Track: {text[:120]}",
        f"{who} promised{when}: {text}"[:600],
        payload,
        session_id=session_id,
        turn_id=turn_id,
    )


def _input_message(store, session_id, turn_id) -> str | None:
    if not turn_id or not access.owner_text(store, turn_id).strip():
        return None
    with store._connect() as db:
        row = db.execute(
            "SELECT m.id FROM turn_messages t JOIN messages m ON m.id=t.message_id "
            "WHERE t.turn_id=? AND t.purpose='input' AND m.role='user' AND m.session_id=?",
            (turn_id, session_id),
        ).fetchone()
    return row["id"] if row else None
