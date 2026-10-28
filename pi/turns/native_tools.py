"""Conker's own tools: a cron job, a tool draft, a memory or a question, under his access layers.

They are offered next to the owner's published tools and follow the same layers (chat > agent >
tool default, see `pi.turns.access`). Remembering and scheduling are Keep: allowed by default, done
at once and listed in Inbox Handled. A tool draft, and anything his settings say to ask about, is
filed as a change request (`pi.agents.change_requests`) that he approves in the Inbox. Every one is
recorded as a request either way. Nothing here acts outside Pi's own store and memory (ADR-0005).
The companion gets them all; another agent gets those selected for it, like any tool. A question
(`conker.ask`) is always allowed: it goes to his Inbox and his answer comes back to the chat as his
next message (`pi.agents.asks`).
"""

from __future__ import annotations

import re

from pi.actions.toolgate import Tool, ToolResult
from pi.agents import asks, change_requests, workflow_drafts
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
        "his approval; the result says which.",
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
        "auto",
    ),
    Tool(
        DRAFT_TOOL,
        "Draft a new tool",
        "Ask the owner to add a new tool: Conker drafts a workflow built from the existing tools, and the "
        "owner reviews, saves and publishes it. Describe what the tool should do.",
        [
            {
                "name": "description",
                "type": "string",
                "required": True,
                "description": "What the new tool should do",
            }
        ],
        "owner_confirmation",
    ),
    Tool(
        REMEMBER,
        "Remember something",
        "Save one fact to memory, in a full sentence. Depending on the owner's settings it is saved at "
        "once or waits for his approval; the result says which.",
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
        "Ask the owner a question when you are unsure he wants something done. It waits in his "
        "Inbox; his answer arrives later as his next message in this chat. Give 2 to 4 short "
        "options he can tap; he can always write his own answer instead.",
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
                "description": '2 to 4 short answers he can tap, e.g. ["Yes, fix it", "Not now"]',
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
    """Make the request real now; if that cannot happen (memory not connected), it waits."""
    try:
        decided = change_requests.approve(loop.store, request["id"], memory=loop.remember)
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
                "where he can undo it."
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
    access layers allow it). A bad argument is an answer, not a failure."""
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
                        "Asked. The question waits in the owner's Inbox; his answer will arrive "
                        "as his next message in this chat. Tell him in one line that you asked, "
                        "and do not act on it until he answers."
                    ),
                },
                tool_id=tool_id,
            )
        if tool_id == SCHEDULE:
            name = _text(args, "name", 80)
            instructions = _text(args, "instructions", 4000)
            timing, clock, when = _timing(args)
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
            capabilities = [
                {
                    "id": tool.id,
                    "kind": "workflow" if tool.id.startswith("workflow:") else "tool",
                    "name": tool.name[:160],
                    "description": (tool.description or "")[:800],
                    "inputs": [
                        {
                            "name": str(field.get("name", "input"))[:200],
                            "type": str(field.get("type", "string"))[:40],
                            "required": field.get("required") is True,
                            "description": str(field.get("description") or "")[:500],
                        }
                        for field in tool.inputs[:100]
                    ],
                }
                for tool in loop._available_tools(execution)
                if not owns(tool.id) and not tool.id.startswith("workflow:")
            ][:200]
            drafted = workflow_drafts.draft(
                loop.store,
                # Every adapter the router has (local and hosted), as the owner's draft route uses.
                loop.router.adapters(),
                workflow_drafts.Request(description=description, capabilities=capabilities),
            )
            if not drafted["ready"] or drafted["document"] is None:
                # The builder needs more: its question goes back to the conversation.
                return ToolResult(
                    ok=True,
                    result={"status": "needs_detail", "message": drafted["reply"]},
                    tool_id=DRAFT_TOOL,
                )
            document = drafted["document"]
            request = change_requests.file(
                loop.store,
                "tool_draft",
                f"Add the tool “{document['name']}”",
                drafted["reply"][:600],
                {"document": document, "issues": drafted["issues"], "description": description},
                session_id=session_id,
                turn_id=turn_id,
            )
            return _filed(request, tool_id)
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
    except ValueError as invalid:
        return ToolResult(
            ok=False, result={"status": "invalid", "message": str(invalid)}, tool_id=tool_id
        )
    except workflow_drafts.DraftError as failure:
        return ToolResult(
            ok=False, result={"status": "unavailable", "message": failure.message}, tool_id=tool_id
        )
