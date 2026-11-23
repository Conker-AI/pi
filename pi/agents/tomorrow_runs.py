"""Each start Conker makes on its own in the forecast window, and whether it would run (R-U10).

Nothing is run, reserved or changed. Every hold is the runner's own check, asked about a moment
that has not come yet, so the forecast says what the runner would decide then:

- the clock: `jobs.clock_hold` (switched off, no clock, quiet hours skip repeating AI jobs) and
  `jobs.busy` (the last run is not over or waits for the owner's answer);
- an AI job's run: `job_limits.verdict` (allowed triggers, shortest interval, runs a day) and, for a
  job Conker made, `proactive_budget.refusal` named by `agent_jobs.held_for` (proactivity off or
  urgent only, quiet hours, the daily suggestion budget);
- the proposal pass: `proposals.due` and the proposals role (`proposals.model_call`), then the
  same budget check; a held pass is tried again at the worker's next check.

Slots are replayed in time order, so a run the forecast lets through counts towards the limits of
the slots after it, as the stored run would. A watched job has no clock: it is one item, checked
every N minutes, held or not by the same rules right now.

Limits, said plainly: each run is assumed to finish before its job's next slot without waiting for
the owner; what a proposal pass reserves (it depends on what it finds) is not counted against jobs,
and a pass may still skip for want of new messages; the owner changing a setting or answering an
approval changes the forecast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pi.core import owner_preferences as prefs
from pi.core import owner_settings
from pi.core.models import Strict
from pi.schedule import agent_jobs, job_limits, jobs, proactive_budget, proposals
from pi.schedule.job_models import Definition

SLOTS = 24  # runs counted per job, as in `tomorrow.MAX_RUNS`
MAX_ITEMS = 200
ONE = prefs.Usage(suggestions=1, researchMinutes=0, costCents=0)


class TomorrowRun(Strict):
    at: float | None  # None for a watch: it starts when its tool has something new
    kind: Literal["clock", "watch", "proposals"]
    jobId: str | None  # None for the proposal pass
    name: str
    everyMinutes: int | None  # a watch's checks
    wouldRun: bool
    reason: str | None  # the runner's code when it would not run


def _instant(at: float) -> datetime:
    return datetime.fromtimestamp(at, UTC)


def _budget(store, at: float, reserved: list[float]) -> str | None:
    reasons = proactive_budget.refusal(store, ONE, now=_instant(at), also=reserved)
    return agent_jobs.held_for(reasons)[0] if reasons else None


def _slots(definition, row, now: float, until: float) -> list[float]:
    """When the clock would start the job: the slot due now or next, then each one after."""
    found, at = [], max(row["next_at"], now)
    while at < until and len(found) < SLOTS:
        found.append(at)
        try:
            at = jobs.next_due(definition, at, anchor=row["created_at"])
        except jobs.JobError:
            break
    return found


def _item(at, kind, job_id, name, reason, every=None) -> dict:
    return {
        "at": at,
        "kind": kind,
        "jobId": job_id,
        "name": name,
        "everyMinutes": every,
        "wouldRun": reason is None,
        "reason": reason,
    }


class _Replay:
    """The runner's checks over the window, with what the forecast's own earlier runs add."""

    def __init__(self, store, rows, now: float, scheduler: bool):
        self.store, self.now, self.scheduler = store, now, scheduler
        self.preferences = prefs.OwnerPreferences.model_validate(prefs.load(store)["preferences"])
        self.reserved: list[float] = []  # suggestions the forecast's earlier runs would reserve
        self.jobs = {}
        with store._connect() as db:
            self.values = owner_settings.read(db)
            for row in rows:
                definition = Definition.model_validate_json(row["definition"])
                earlier = [
                    found["started_at"]
                    for found in db.execute(
                        "SELECT started_at FROM scheduled_runs WHERE job_id=? "
                        "AND status!='skipped' AND started_at>?",
                        (row["id"], now - job_limits.DAY),
                    )
                ]
                self.jobs[row["id"]] = (row, definition, jobs.busy(db, row["id"]), earlier)

    def held(self, job_id: str, at: float, started_by: str) -> str | None:
        """What the runner would say about a start of this job at `at`, as a code; None: it runs."""
        _row, definition, busy, earlier = self.jobs[job_id]
        if not self.scheduler:
            return "SCHEDULER_OFF"
        if started_by == "time":
            quiet = prefs.quiet_now(self.preferences, _instant(at))
            code = jobs.clock_hold(definition, quiet_hours=quiet, hidden=False)
            if code:
                return code
        if busy:
            return "LAST_RUN_OPEN"
        if definition.target.kind != "agent":
            return None
        limit = job_limits.verdict(self.values, started_by, at, earlier)
        if limit:
            return limit[0]
        if definition.createdBy == "conker":
            return _budget(self.store, at, self.reserved)
        return None

    def clock(self, until: float) -> list[dict]:
        starts = []
        for job_id, (row, definition, _busy, _earlier) in self.jobs.items():
            if jobs.clock_hold(definition, quiet_hours=False, hidden=False) is None:
                starts += [(at, job_id) for at in _slots(definition, row, self.now, until)]
        items = []
        for at, job_id in sorted(starts):
            _row, definition, _busy, earlier = self.jobs[job_id]
            reason = self.held(job_id, at, "time")
            if reason is None:
                earlier.append(at)
                if definition.target.kind == "agent" and definition.createdBy == "conker":
                    self.reserved.append(at)
            items.append(_item(at, "clock", job_id, definition.name, reason))
        return items

    def watches(self) -> list[dict]:
        every = job_limits.min_minutes(self.store)
        items = []
        for job_id, (_row, definition, _busy, _earlier) in self.jobs.items():
            if definition.enabled and definition.watch is not None:
                minutes = max(definition.watch.everyMinutes, every)
                reason = self.held(job_id, self.now, "watch")
                items.append(_item(None, "watch", job_id, definition.name, reason, minutes))
        return items

    def proposals(self, until: float, worker) -> list[dict]:
        """The proposal pass, when its worker runs: when it is due, and each change of outcome."""
        if worker is None:
            return []
        every, check = worker.interval_seconds, worker.check_seconds
        enabled, _complete = proposals.model_call(self.store, None)
        at = self.now
        while at < until and not proposals.due(self.store, every, now=at):
            at += check
        items, last = [], None
        while at < until and len(items) < SLOTS:
            if not enabled:
                reason = "ROLE_OFF"
            else:
                spent = [moment for moment in self.reserved if moment <= at]
                reason = _budget(self.store, at, spent)
            if reason is None or reason != last:
                items.append(_item(at, "proposals", None, "Ideas pass", reason))
            last = reason
            # A pass held by the budget or quiet hours is tried again at the next check.
            at += check if reason not in (None, "ROLE_OFF") else every
        return items


def planned(store, rows, now: float, until: float, *, scheduler=True, proposal_worker=None):
    """Every start in [now, until), soonest first; watches last. `rows`: scheduled_jobs rows."""
    replay = _Replay(store, rows, now, scheduler)
    items = replay.clock(until) + replay.proposals(until, proposal_worker) + replay.watches()
    items.sort(key=lambda item: (item["at"] is None, item["at"] or 0, item["name"]))
    return items[:MAX_ITEMS]
