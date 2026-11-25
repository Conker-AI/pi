"""The Morning check-in's fixed job id and the action id of each run's calendar read.

In core because the job (`pi.schedule`), the read (`pi.context.calendar_events`) and forgetting
(`pi.actions.tool_forgets`) all need them and cannot import each other. The read is made before
the run's turn, so it is no tool action of a turn: forgetting finds it from the run instead, by
this id, and asks ToolGate to blank its receipt (the events' titles and places) with the chat.
"""

from __future__ import annotations

import hashlib

# `pi.schedule.agent_jobs.BUILT_IN` and `pi.schedule.rhythm.MORNING_CHECK_IN` take it from here.
MORNING_CHECK_IN = "job_beca06ba345a8b3170a4d8e2473fe00f"


def calendar_action(run_id: str) -> str:
    """The read's action id for one run: a repeated dispatch gets the same receipt back, and
    forgetting the run's chat can name it."""
    return "pi-calendar-" + hashlib.sha256(run_id.encode()).hexdigest()[:40]
