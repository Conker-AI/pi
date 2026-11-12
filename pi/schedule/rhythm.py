"""The daily rhythm Pi ships: AI jobs a fresh Conker has without the owner designing one.

Today that is one job, the Morning check-in: daily at 08:00 in the owner's time zone (the zone of
his quiet hours, read once when it is made), the companion looks at what needs him today and tells
him in a few lines, or replies NOTHING and stays quiet (`agent_jobs.QUIET`). It is an ordinary AI
job: he edits, pauses or adopts it like any other.

Who made it: `createdBy: "conker"`. Its instructions are fixed text Pi ships, neither typed nor
approved by the owner, so they must never count as his words for approvals
(`pi.turns.access.owner_text` returns "" for its runs), and each run spends one suggestion of his
daily proactive budget, so his quiet hours, his proactivity switch ("off" holds every run back
until he turns it on again) and his daily limit all apply. Making it does not use Conker's own
allowance of jobs a day (`agent_jobs.BUILT_IN`). If he wants it to run as his own, he adopts it
(`POST /jobs/{id}/adopt`).

Seeded once per install, on new and upgraded installs alike, when Pi starts with its scheduler
(`pi.api.startup`): without the scheduler no job runs, so nothing is made there, and the many
tests that start the app see no extra job. The owner setting `rhythm.seeded` records that the seed
ran and names the job; the job's fixed id (`MORNING_CHECK_IN`) is a second record, since jobs are
paused, never deleted. Either one stops a second seed, so turning the job off, or resetting the
settings document, never brings it back. The UI tells the built-in job apart by that id.
"""

from __future__ import annotations

import sqlite3

from pi.owner import settings
from pi.schedule import agent_jobs
from pi.schedule.job_models import AgentJobCreate, Timing

SEEDED = "rhythm.seeded"
MORNING_CHECK_IN = agent_jobs.BUILT_IN[0]
NAME = "Morning check-in"
INSTRUCTIONS = (
    "Morning check-in. Look at what needs the owner today: open items in his Inbox (approvals, "
    "questions, notes), open questions and things he or you promised, plans, dates and deadlines "
    "you remember for today or the next few days, and cron jobs whose last run failed. Only read; "
    "change nothing. If something needs him, tell him in a few short lines, the most important "
    "first, each with what he can do about it. If nothing needs him today, reply with only "
    f"{agent_jobs.QUIET}."
)
DAILY_AT_EIGHT = Timing(kind="daily", time="08:00", day=0, hours=24)


def seed(store, now=None) -> dict | None:
    """Make the Morning check-in once; the new job, or None when the seed ran before."""
    if settings.value(store, SEEDED) is not None:
        return None
    with store._connect() as db:
        made = db.execute("SELECT 1 FROM scheduled_jobs WHERE id=?", (MORNING_CHECK_IN,)).fetchone()
    if made:
        # The settings document was reset; the job is still there, so only the record comes back.
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            settings.put_in(db, SEEDED, {"morningCheckIn": MORNING_CHECK_IN})
            db.commit()
        return None
    body = AgentJobCreate(
        name=NAME,
        instructions=INSTRUCTIONS,
        agentId="companion",
        timing=DAILY_AT_EIGHT,
        timeZone=agent_jobs.owner_time_zone(store),
    )
    try:
        return agent_jobs.create(
            store,
            body,
            now,
            created_by="conker",
            identity=MORNING_CHECK_IN,
            then=lambda db, job: settings.put_in(db, SEEDED, {"morningCheckIn": job["id"]}),
        )
    except sqlite3.IntegrityError:
        return None  # Another start made it first; its transaction recorded the seed.
