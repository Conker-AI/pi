"""Conker's own tools: a cron job, a tool draft, a memory or a question, under his access layers.

They are offered next to the owner's published tools and follow the same layers (chat > agent >
tool default, see `pi.turns.access`). Remembering is Keep: allowed by default, done at once and
listed in Inbox Handled. Scheduling asks by default: a recurring job runs instructions the model
wrote, so it is filed as a change request (`pi.agents.change_requests`) he approves in the Inbox,
unless his settings for an agent or a chat loosen it. A tool draft is always filed: the request is
made at once and Pi drafts it in the background (`pi.turns.tool_drafting`), so the chat answers
without waiting. Every one is recorded as a request either way. Nothing here acts outside Pi's own
store and memory (ADR-0005).
The companion gets them all; another agent gets those selected for it, like any tool. A private
chat (memory off, or inheriting it) is never offered remembering or scheduling: neither may carry
what was said there into memory or into a job. A run of an AI job is never offered scheduling or a
tool draft, so no job makes another. Both are refused again when called. A question (`conker.ask`)
is always allowed: it goes to his Inbox and his answer comes back to the chat as his next message
(`pi.agents.asks`).
"""

from __future__ import annotations

import re

from pi.actions.toolgate import Tool, ToolResult
from pi.agents import asks, change_requests, tool_drafts
from pi.agents.profiles import AgentError
from pi.conversation import settings as session_settings
from pi.schedule import agent_jobs

SCHEDULE = "conker.schedule"
DRAFT_TOOL = "conker.draft_tool"
REMEMBER = "conker.remember"
ASK = "conker.ask"
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

CATALOG = (
    Tool(
        SCHEDULE,
        "Schedule an AI job",
        "Create a cron job: an AI job that carries out instructions with tools on a schedule or when "
        "its webhook is called. Depending on the owner's settings it is created at once or waits for "
        "their approval; the result says which.",
        [
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Short name, e.g. Weekly life review",
            },
            {
                "name": "instructions",
                "type": "string",
                "required": True,
                "description": "What the job does each run, including any conditions",
            },
            {
                "name": "repeat",
                "type": "string",
                "required": True,
                "description": "hours, daily, weekly or webhook",
            },
            {
                "name": "hours",
                "type": "number",
                "required": False,
                "description": "For repeat=hours: 1 to 24",
            },
            {
                "name": "time",
                "type": "string",
                "required": False,
                "description": "For daily or weekly: HH:MM, 24-hour",
            },
            {
                "name": "day",
                "type": "string",
                "required": False,
                "description": "For weekly: sunday to saturday",
            },
        ],
        "owner_confirmation",
    ),
    Tool(
        DRAFT_TOOL,
        "Draft a new tool",
        "Use this whenever the owner asks for a new tool, an automation or a workflow, instead of "
        "doing the job yourself. Pi drafts it from the existing tools in the background and puts "
        "the draft in the owner's Inbox, where they review and publish it. Describe what the tool "
        "should do, in the owner's words.",
        [
            {
                "name": "description",
                "type": "string",
                "required": True,
                "description": "What the new tool should do, in the owner's words",
            }
        ],
        "owner_confirmation",
    ),
    Tool(
        REMEMBER,
        "Remember something",
        "Save one fact to memory, in a full sentence. Depending on the owner's settings it is saved at "
        "once or waits for their approval; the result says which.",
        [
            {
                "name": "text",
                "type": "string",
                "required": True,
                "description": "The fact to remember, e.g. The owner's physics exam is on Thursday 30 October",
            }
        ],
        "auto",
    ),
    Tool(
        ASK,
        "Ask the owner",
        "Ask the owner a question when you are unsure they want something done. It waits in their "
        "Inbox; their answer arrives later as their next message in this chat. Give 2 to 4 short "
        "options they can tap; they can always write their own answer instead.",
        [
            {
                "name": "question",
                "type": "string",
                "required": True,
                "description": "One clear question, e.g. Your server app's login bothers you. Fix it today?",
            },
            {
                "name": "options",
                "type": "array",
                "required": False,
                "description": '2 to 4 short answers they can tap, e.g. ["Yes, fix it", "Not now"]',
            },
        ],
        "auto",
    ),
)
IDS = {tool.id for tool in CATALOG}


def catalog(execution) -> list[Tool]:
    if not execution or execution.get("kind") == "companion":
        return list(CATALOG)
    selected = set(execution["configuration"]["toolIds"])
    return [tool for tool in CATALOG if tool.id in selected]


def owns(tool_id: str) -> bool:
    return tool_id in IDS


# Never offered, and refused, in a private chat or in a run of an AI job.
PRIVATE_REFUSED = {REMEMBER, SCHEDULE}
JOB_REFUSED = {SCHEDULE, DRAFT_TOOL}


def private(store, session_id, execution) -> bool:
    """Memory is off for this turn: in its frozen settings, the chat's own, or a floor it inherits
    from messages it carries. Anything unreadable counts as private."""
    if ((execution or {}).get("privacy") or {}).get("memoryDisabled") not in (False, None):
        return True
    if not session_id:
        return False
    try:
        with store._connect() as db:
            db.execute("BEGIN")
            floor = session_settings.inherited_privacy(db, session_id)
            live = session_settings.source_privacy(db, session_id)
    except AgentError:
        return True
    return floor["memoryDisabled"] or live is None or bool(live["memoryDisabled"])


def job_run(store, turn_id) -> bool:
    with store._connect() as db:
        return agent_jobs.run_of(db, turn_id) is not None


def refused(store, tool_id, *, session_id, turn_id, execution) -> str | None:
    """Why Conker may not use this tool in this turn, in words it can pass on; None if it may."""
    if tool_id in PRIVATE_REFUSED and private(store, session_id, execution):
        return (
            "This chat is private (memory off), so nothing from it is remembered or scheduled. "
            "Tell the owner they can do it from a chat with memory on."
        )
    if tool_id in JOB_REFUSED and job_run(store, turn_id):
        return (
            "A scheduled AI job cannot make another job or draft a tool. If one is needed, say "
            "so in the reply and the owner can ask for it."
        )
    return None


def offered(loop, execution, session_id, turn_id) -> list[Tool]:
    """Conker's own tools for this turn: the agent's selection, minus what the turn may not use."""
    return [
        tool
        for tool in catalog(execution)
        if not refused(
            loop.store, tool.id, session_id=session_id, turn_id=turn_id, execution=execution
        )
    ]


def _text(args, name, limit, required=True):
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ValueError(f"Give {name} as text.")
        return None
    return value.strip()[:limit]


def _timing(args) -> tuple[dict, bool, str]:
    repeat = str(args.get("repeat", "")).strip().lower()
    if repeat == "hours":
        hours = args.get("hours")
        if not isinstance(hours, (int, float)) or not 1 <= int(hours) <= 24:
            raise ValueError("For repeat=hours give hours from 1 to 24.")
        return (
            {"kind": "interval", "time": "00:00", "day": 0, "hours": int(hours)},
            True,
            f"every {int(hours)} hours",
        )
    if repeat == "webhook":
        return (
            {"kind": "interval", "time": "00:00", "day": 0, "hours": 24},
            False,
            "when its webhook is called",
        )
    time = str(args.get("time", "")).strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time):
        raise ValueError("For daily or weekly give time as HH:MM.")
    if repeat == "daily":
        return {"kind": "daily", "time": time, "day": 0, "hours": 24}, True, f"every day at {time}"
    if repeat == "weekly":
        day = str(args.get("day", "")).strip().lower()
        if day not in DAYS:
            raise ValueError("For weekly give day as sunday to saturday.")
        return (
            {"kind": "weekly", "time": time, "day": DAYS.index(day), "hours": 24},
            True,
            f"every {day.title()} at {time}",
        )
    raise ValueError("repeat must be hours, daily, weekly or webhook.")


def _done(loop, request, tool_id) -> ToolResult:
    """Make the request real now in Conker's name; if that cannot happen (memory not connected,
    Conker's daily job limit reached), it waits for the owner."""
    try:
        decided = change_requests.approve(
            loop.store, request["id"], memory=loop.remember, by="conker"
        )
    except change_requests.ChangeRequestError:
        return _filed(request, tool_id)
    if decided["state"] != "approved":
        return _filed(request, tool_id)
    title = request["title"].rstrip(".")
    return ToolResult(
        ok=True,
        result={
            "status": "done",
            "requestId": request["id"],
            "made": decided["result"],
            "message": (
                f"Done: \u201c{title}\u201d. It is listed in the owner's Inbox under Handled, "
                "where they can undo it."
            ),
        },
        tool_id=tool_id,
    )


def _filed(request, tool_id) -> ToolResult:
    title = request["title"].rstrip(".")
    return ToolResult(
        ok=True,
        result={
            "status": "waiting_for_owner",
            "requestId": request["id"],
            "message": (
                f"Not done yet: “{title}” is waiting in the owner's Inbox and exists only "
                "if they approve it there. Tell them it is waiting for their approval; do not say "
                "it is done."
            ),
        },
        tool_id=tool_id,
    )


def call(loop, tool_id, args, *, session_id, turn_id, execution, run=False) -> ToolResult:
    """Validate the arguments and file one change request, made real at once when `run` (his
    access layers allow it). A bad argument, or a tool this turn may not use, is an answer, not a
    failure."""
    why = refused(loop.store, tool_id, session_id=session_id, turn_id=turn_id, execution=execution)
    if why:
        return _refusal(tool_id, why)
    try:
        if tool_id == ASK:
            asked = asks.file(
                loop.store, session_id, turn_id, args.get("question"), args.get("options")
            )
            return ToolResult(
                ok=True,
                result={
                    "status": "asked",
                    "askId": asked["id"],
                    "options": asked["options"],
                    "message": (
                        "Asked. The question waits in the owner's Inbox; their answer will arrive "
                        "as their next message in this chat. Tell them in one line that you asked, "
                        "and do not act on it until they answer."
                    ),
                },
                tool_id=tool_id,
            )
        if tool_id == SCHEDULE:
            name = _text(args, "name", 80)
            instructions = _text(args, "instructions", 4000)
            timing, clock, when = _timing(args)
            full = agent_jobs.room(loop.store, "owner")
            if full:
                return _refusal(tool_id, full)
            agent = execution.get("agentId") if execution else None
            payload = {
                "name": name,
                "instructions": instructions,
                "agentId": agent or "companion",
                "timing": timing,
                "timeZone": agent_jobs.owner_time_zone(loop.store),
                "enabled": True,
                "clock": clock,
            }
            request = change_requests.file(
                loop.store,
                "ai_job",
                f"Create the AI job “{name}”, {when}",
                instructions[:600],
                payload,
                session_id=session_id,
                turn_id=turn_id,
            )
            return _done(loop, request, tool_id) if run else _filed(request, tool_id)
        if tool_id == DRAFT_TOOL:
            description = _text(args, "description", 4000)
            request = tool_drafts.file(
                loop.store, description, session_id=session_id, turn_id=turn_id
            )
            drafts = getattr(loop, "drafts", None)
            if drafts is not None:
                drafts.kick()
            return ToolResult(
                ok=True,
                result={
                    "status": "drafting",
                    "requestId": request["id"],
                    "message": (
                        "Drafting has started in the background. The draft will be in the "
                        "owner's Inbox in a few minutes, where they review it; nothing exists "
                        "or runs until they publish it. Tell them in one short sentence that "
                        "you are drafting it and it will be in their Inbox. Do not do the job "
                        "yourself and do not say the tool is ready."
                    ),
                },
                tool_id=tool_id,
            )
        text = _text(args, "text", 2000)
        request = change_requests.file(
            loop.store,
            "memory",
            f"Remember: {text[:140]}",
            text,
            {"text": text},
            session_id=session_id,
            turn_id=turn_id,
        )
        return _done(loop, request, tool_id) if run else _filed(request, tool_id)
    except change_requests.Full as full:
        return _refusal(tool_id, str(full))
    except ValueError as invalid:
        return ToolResult(
            ok=False, result={"status": "invalid", "message": str(invalid)}, tool_id=tool_id
        )


def _refusal(tool_id, message) -> ToolResult:
    return ToolResult(ok=False, result={"status": "refused", "message": message}, tool_id=tool_id)
