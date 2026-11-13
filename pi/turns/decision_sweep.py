"""Decisions made in ToolGate that Pi was not told about continue the work anyway.

The owner's Inbox and `conker apply approvals` tell Pi right after ToolGate records his decision
(`gateway/answers.py` -> `/approvals/{id}/decided`). A decision made with ToolGate's admin key
(its own dashboard, `POST /v2/requests/{id}/decision`), or one made while Pi was down, reaches
nobody, and the parked chat turn or cron job run waits for ever. Every `interval` seconds this
worker reads, with Pi's own key, the status of each request Pi still waits on, and hands a
decided one to `answers.decided`: the same step the owner route takes. Pi grants nothing:
ToolGate checks the approval when it is spent, and a second caller finds the turn or run
already taken (`claim_turn`, `dispatch_claim`), so racing the owner route is harmless.

Only the decision is read, never its note: a note reaches the chat as his words only through
the owner's own channel, so a "no" here just closes.
"""

from __future__ import annotations

import logging
import threading

from pi.schedule import jobs
from pi.turns import answers

log = logging.getLogger(__name__)

DECIDED = {"approved", "rejected", "dismissed"}


def waiting(db, limit) -> list[tuple[str, str | None]]:
    """(request id, agent that asked) for parked turns and tool cron runs. A random `limit` of
    each per pass, so many that stay undecided never keep a newer one from being read."""
    settings = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_settings'"
    ).fetchone()
    agent = "json_extract(s.snapshot,'$.agentId')" if settings else "NULL"
    join = "LEFT JOIN turn_settings s ON s.turn_id=t.id " if settings else ""
    found = [
        (row[0], row[1] or "companion")
        for row in db.execute(
            f"SELECT t.approval_request_id, {agent} FROM turns t {join}"
            "WHERE t.status='awaiting_approval' AND t.approval_request_id IS NOT NULL "
            "ORDER BY random() LIMIT ?",
            (limit,),
        )
    ]
    # A cron job's request was made by Pi's key itself (PublishedJobs), so no agent.
    found += [
        (row[0], None)
        for row in db.execute(
            "SELECT json_extract(receipt,'$.request_id') FROM scheduled_runs "
            "WHERE status='awaiting_approval' AND json_valid(receipt) "
            "AND json_extract(receipt,'$.request_id') IS NOT NULL ORDER BY random() LIMIT ?",
            (limit,),
        )
    ]
    return found


def sweep(store, toolgate, loop, executor=None, limit=50) -> int:
    """Continue each decided request; how many were handed on."""
    if toolgate is None or not getattr(toolgate, "execution_key", ""):
        return 0
    with store._connect() as db:
        pending = waiting(db, limit)
    handed = 0
    for request_id, agent in pending:
        status = toolgate.request_status(request_id, agent)
        if status not in DECIDED:
            continue
        result = answers.decided(
            loop,
            request_id,
            status,
            "",
            resume_run=(
                (lambda run_id: jobs.dispatch_claim(store, {"id": run_id}, executor, resume=True))
                if executor is not None
                else None
            ),
            cancel_run=lambda run_id: jobs.cancel_run(store, run_id),
        )
        handed += result.get("continued") != "nothing_waiting"
    return handed


class DecisionSweep:
    def __init__(self, store, toolgate, loop, executor=None, interval=60.0):
        if interval <= 0:
            raise ValueError("Decision sweep interval must be positive.")
        self.store, self.toolgate, self.loop, self.executor = store, toolgate, loop, executor
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                sweep(self.store, self.toolgate, self.loop, self.executor)
            except Exception:
                log.error("Reading ToolGate decisions paused; the waiting work stays as it is.")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pi-decision-sweep", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
