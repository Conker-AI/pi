"""Conker's own tools: it prepares a cron job, a tool or a memory, and the owner verifies each.

They are offered next to the owner's published tools. Calling one files a change request (see
`pi.agents.change_requests`) and answers at once; nothing is created until the owner approves it in
the Inbox, so a prompt attack can at worst leave a request the owner declines. Nothing here acts
outside Pi's own store (ADR-0005). The companion gets them all; another agent gets those selected
for it, like any tool.
"""

from __future__ import annotations

import re

from pi.actions.toolgate import Tool, ToolResult
from pi.agents import change_requests, workflow_drafts
from pi.schedule import agent_jobs

SCHEDULE = "conker.schedule"
DRAFT_TOOL = "conker.draft_tool"
REMEMBER = "conker.remember"
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

CATALOG = (
    Tool(
        SCHEDULE,
        "Schedule an AI job",
        "Ask the owner to create a cron job: an AI job that carries out instructions with tools on a "
        "schedule or when its webhook is called. The owner must approve it in the Inbox before it exists.",
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
    ),
    Tool(
        REMEMBER,
        "Remember something",
        "Ask the owner to save one fact to memory, in a full sentence. The owner approves it in the Inbox.",
        [
            {
                "name": "text",
                "type": "string",
                "required": True,
                "description": "The fact to remember, e.g. The owner's physics exam is on Thursday 30 October",
            }
        ],
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


def call(loop, tool_id, args, *, session_id, turn_id, execution) -> ToolResult:
    """Validate the arguments and file one change request; a bad argument is an answer, not a failure."""
    try:
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
            return _filed(request, tool_id)
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
        return _filed(request, tool_id)
    except ValueError as invalid:
        return ToolResult(
            ok=False, result={"status": "invalid", "message": str(invalid)}, tool_id=tool_id
        )
    except workflow_drafts.DraftError as failure:
        return ToolResult(
            ok=False, result={"status": "unavailable", "message": failure.message}, tool_id=tool_id
        )
