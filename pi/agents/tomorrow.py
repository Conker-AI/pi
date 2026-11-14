"""What Conker will do on its own in the next day (R-U10): a read-only forecast.

Built only from what Pi already keeps, nothing is run or changed:

- `jobs`: cron and AI jobs the clock will start inside the window, with each one's next time and
  how often it fires (at most `MAX_RUNS` counted), and whether it is held, because its last run
  still waits for his answer (the next slot is then skipped, see `jobs.WAITING_TURN`). A watched
  job has no clock: it is listed as `watch` and fires when its tool has something new.
- `approvals`: parked chat turns whose approval runs out inside the window; Pi ends such a turn
  itself shortly after (pi.turns.decision_sweep), so "it expired and nothing ran" is what tomorrow
  holds for them. Tool arguments and his words are never returned.

A job from a chat that is forgotten or in the Trash is not listed (it does not run) and neither is
a parked turn of such a chat. Limit: repeating AI jobs skip quiet hours at run time; the forecast
does not know them, so it may list a slot that is then skipped.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pi.conversation import visibility
from pi.core.models import Strict
from pi.schedule import jobs
from pi.schedule.job_models import Definition

HOUR = 3600.0
MAX_RUNS = 24  # slots counted per job; a job that fires more often reports this many
MAX_ITEMS = 100


class TomorrowJob(Strict):
    id: str
    name: str
    trigger: Literal["clock", "watch"]
    createdBy: Literal["owner", "conker"]
    nextAt: float | None
    runs: int | None
    held: bool


class TomorrowApproval(Strict):
    sessionId: str
    toolId: str | None
    expiresAt: float


class Tomorrow(Strict):
    schemaVersion: Literal[1]
    since: float
    until: float
    hours: int
    jobs: list[TomorrowJob]
    runsPlanned: int
    approvals: list[TomorrowApproval]


def _slots(definition: Definition, first: float, until: float, anchor: float) -> int:
    """How many times the clock fires from `first` (inclusive) to before `until`."""
    count, at = 0, first
    while at < until and count < MAX_RUNS:
        count += 1
        try:
            at = jobs.next_due(definition, at, anchor=anchor)
        except jobs.JobError:
            break
    return count


def _held(db, job_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM scheduled_runs WHERE job_id=? "
            f"AND (status NOT IN ('completed','failed','cancelled') OR ({jobs.WAITING_TURN}))",
            (job_id,),
        ).fetchone()
        is not None
    )


def _job(db, row, now: float, until: float) -> dict | None:
    definition = Definition.model_validate(json.loads(row["definition"]))
    if not definition.enabled:
        return None
    clockless = definition.target.kind == "agent" and not definition.target.clock
    if definition.watch is not None:
        trigger, runs, at = "watch", None, None
    elif clockless or row["next_at"] >= until:
        return None
    else:
        trigger, at = "clock", max(row["next_at"], now)
        runs = _slots(definition, row["next_at"], until, row["created_at"])
    return {
        "id": row["id"],
        "name": definition.name,
        "trigger": trigger,
        "createdBy": definition.createdBy,
        "nextAt": at,
        "runs": runs,
        "held": _held(db, row["id"]),
    }


def _expires(value) -> float | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _approvals(db, until: float) -> list[dict]:
    rows = db.execute(
        "SELECT session_id, approval_tool_id, approval_expires_at FROM turns "
        "WHERE status='awaiting_approval' AND approval_expires_at IS NOT NULL "
        f"AND NOT {visibility.hidden_sql('session_id')} ORDER BY started_at LIMIT 500"
    ).fetchall()
    found = []
    for row in rows:
        expires = _expires(row["approval_expires_at"])
        if expires is not None and expires < until:
            found.append(
                {
                    "sessionId": row["session_id"],
                    "toolId": row["approval_tool_id"],
                    "expiresAt": expires,
                }
            )
    return sorted(found, key=lambda item: item["expiresAt"])[:MAX_ITEMS]


def tomorrow(store, now: float, hours: int) -> dict:
    """Jobs the clock starts and approvals that run out between `now` and `hours` from now."""
    until = now + hours * HOUR
    with store._connect() as db:
        rows = db.execute(
            f"SELECT * FROM scheduled_jobs WHERE NOT {jobs.PAUSED} ORDER BY next_at, id"
        ).fetchall()
        listed = [item for row in rows if (item := _job(db, row, now, until))]
        approvals = _approvals(db, until)
    listed.sort(key=lambda item: (item["nextAt"] is None, item["nextAt"] or 0, item["id"]))
    return {
        "schemaVersion": 1,
        "since": now,
        "until": until,
        "hours": hours,
        "jobs": listed[:MAX_ITEMS],
        "runsPlanned": sum(item["runs"] or 0 for item in listed),
        "approvals": approvals,
    }
