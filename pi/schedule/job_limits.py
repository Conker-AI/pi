"""The owner's limits on how AI jobs start: allowed triggers, shortest interval and runs a day.

Three settings in his settings document hold a run back before any model is asked, the way quiet
hours do (`agent_jobs.run`): a run is skipped, never failed.

- `proactivity.jobs.triggers`: a run started by a trigger he left out (a time, a tool's answer
  changing, a webhook) does not run. "Something in Conker" has no trigger of its own yet.
- `proactivity.jobs.min_interval`: a run the clock starts within that many minutes of the job's
  last run does not run, and a watch never looks more often (`agent_triggers.check`). Webhook
  calls keep their own limits (`agent_hooks`).
- `proactivity.jobs.max_runs`: a job that already ran that many times in the last 24 hours makes
  no more automatic runs.

The owner's own Run now is never held, though it counts as a run. A value of the wrong type or
out of the registry's range keeps the default.
"""

from __future__ import annotations

from pi.core import owner_settings as owner_settings

TRIGGERS = "proactivity.jobs.triggers"
MIN_INTERVAL = "proactivity.jobs.min_interval"
MAX_RUNS = "proactivity.jobs.max_runs"
ALL_TRIGGERS = ("time", "watch", "webhook", "events")
MIN_INTERVAL_RANGE, MIN_INTERVAL_DEFAULT = (1, 1440), 5
MAX_RUNS_RANGE, MAX_RUNS_DEFAULT = (1, 1000), 100
DAY = 86400


def whole(value, bounds, default) -> int:
    """The owner's whole number in bounds, or the default for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return default
    return int(value) if bounds[0] <= value <= bounds[1] else default


def allowed_triggers(values: dict) -> set[str]:
    """The triggers the owner allows; every one for a value that is not a list."""
    value = values.get(TRIGGERS)
    if not isinstance(value, list):
        return set(ALL_TRIGGERS)
    return {item for item in value if item in ALL_TRIGGERS}


def min_minutes(store) -> int:
    """The shortest interval between a job's runs, in minutes."""
    value = owner_settings.value(store, MIN_INTERVAL)
    return whole(value, MIN_INTERVAL_RANGE, MIN_INTERVAL_DEFAULT)


def held(store, run_id: str, job_id: str, started_by: str) -> tuple[str, str] | None:
    """Why the owner's job limits hold this run back, as (code, words), or None.

    `started_by` is time, watch, webhook or owner (his Run now, never held).
    """
    if started_by == "owner":
        return None
    with store._connect() as db:
        values = owner_settings.read(db)
        row = db.execute("SELECT started_at FROM scheduled_runs WHERE id=?", (run_id,)).fetchone()
        started = None if row is None else row["started_at"]
        earlier = []
        if started is not None:
            earlier = [
                found["started_at"]
                for found in db.execute(
                    "SELECT started_at FROM scheduled_runs WHERE job_id=? AND id!=? "
                    "AND status!='skipped' AND started_at>? AND started_at<=?",
                    (job_id, run_id, started - DAY, started),
                )
            ]
    return verdict(values, started_by, started, earlier)


def verdict(
    values: dict, started_by: str, started: float | None, earlier
) -> tuple[str, str] | None:
    """`held` from what it read: the owner's settings `values`, when the run starts and when the
    job's other runs that were not skipped started (`earlier`). The forecast (`pi.agents.tomorrow`)
    asks it about runs that have not happened yet. A run that is not found is held only by its
    trigger."""
    if started_by == "owner":
        return None
    if started_by not in allowed_triggers(values):
        return "TRIGGER_OFF", "the owner does not let this kind of trigger start a job."
    if started is None:
        return None
    if started_by == "time":
        gap = whole(values.get(MIN_INTERVAL), MIN_INTERVAL_RANGE, MIN_INTERVAL_DEFAULT)
        if any(started - gap * 60 < at <= started for at in earlier):
            return "TOO_SOON", f"the job ran less than {gap} minutes ago."
    most = whole(values.get(MAX_RUNS), MAX_RUNS_RANGE, MAX_RUNS_DEFAULT)
    if sum(1 for at in earlier if started - DAY < at <= started) >= most:
        return "RUN_LIMIT", f"the job already ran {most} times in the last day."
    return None
