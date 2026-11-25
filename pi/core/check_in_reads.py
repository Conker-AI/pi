"""The Morning check-in's fixed job id and the action ids of each run's calendar and mail reads.

In core because the job (`pi.schedule`), the reads (`pi.context.calendar_events`,
`pi.context.recent_mail`) and forgetting (`pi.actions.tool_forgets`) all need them and cannot
import each other. The reads are made before the run's turn, so they are no tool actions of a
turn: forgetting finds them from the run instead, by these ids, and asks ToolGate to blank their
receipts (the events' titles and places, the mail's senders, subjects and snippets) with the chat.
"""

from __future__ import annotations

import hashlib

# `pi.schedule.agent_jobs.BUILT_IN` and `pi.schedule.rhythm.MORNING_CHECK_IN` take it from here.
MORNING_CHECK_IN = "job_beca06ba345a8b3170a4d8e2473fe00f"


def calendar_action(run_id: str) -> str:
    """The read's action id for one run: a repeated dispatch gets the same receipt back, and
    forgetting the run's chat can name it."""
    return "pi-calendar-" + hashlib.sha256(run_id.encode()).hexdigest()[:40]


def mail_action(run_id: str) -> str:
    """The mail read's action id for one run, made the same way."""
    return "pi-mail-" + hashlib.sha256(run_id.encode()).hexdigest()[:40]


def actions(run_id: str) -> tuple[str, ...]:
    """Every read a check-in run can make before its turn: what forgetting its chat must name."""
    return calendar_action(run_id), mail_action(run_id)
