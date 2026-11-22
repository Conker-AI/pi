"""The words around an AI job's run: the date line, the quiet reply and the Inbox note.

Split out of `pi.schedule.agent_jobs` as pure moves; nothing here decides whether a job runs.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from pi.core import notices

QUIET = "NOTHING"
# English names, whatever the server's locale.
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
MAX_TITLE = 160
MAX_DETAIL = 400
# A note is the model's text shown to the owner: no terminal or other control codes.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def when(instant: float, zone: str) -> str:
    """The run's date, weekday and time in the job's time zone, so "today", "this week" or "the
    first week of the month" in its instructions mean something. Pi's words, never the owner's."""
    local = datetime.fromtimestamp(instant, ZoneInfo(zone))
    return (
        f"This run started on {DAYS[local.weekday()]}, {local.day} {MONTHS[local.month - 1]} "
        f"{local.year} ({local:%Y-%m-%d}) at {local:%H:%M}, {zone} time (the job's time zone)."
    )


def summarize(text: str) -> tuple[str, str] | None:
    """The first line says what needs the owner; NOTHING (or an empty reply) stays quiet."""
    lines = [line.strip() for line in CONTROL.sub("", text).strip().splitlines() if line.strip()]
    if not lines or lines[0].strip(" .*_#`").upper() == QUIET:
        return None
    title = re.sub(r"^[#>*\-\s]+|[*_`]+", "", lines[0]).strip()[:MAX_TITLE]
    detail = " ".join(re.sub(r"[*_`#>]+", "", line) for line in lines[1:])[:MAX_DETAIL]
    return title or "Your AI job has something for you", detail


def _note(store, run_id, job_id, session_id, message_id, title, detail, now):
    with store._connect() as db:
        # One note per run: a replayed run adds nothing.
        notices.insert(
            db,
            identity="note_" + uuid.uuid4().hex,
            kind=notices.AGENT_JOB,
            subject=run_id,
            title=title,
            detail=detail,
            session_id=session_id,
            facts={"jobId": job_id, "messageId": message_id},
            created_at=now,
        )
