"""Opt-in schedule worker; durable admission remains in jobs, effects in ToolGate."""

from __future__ import annotations

import logging
import threading

from . import jobs

log = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, store, adapter, interval=5.0):
        if interval <= 0:
            raise ValueError("Scheduler interval must be positive.")
        self.store, self.adapter, self.interval = store, adapter, interval
        self._stop = threading.Event()
        self._thread = None
        self._budget_cursor = ""

    def tick(self, now=None):
        claimed = jobs.claim_due(self.store, now=now)
        newly_held = {
            item["id"] for item in claimed if item["definition"].get("requireBudget", False)
        }
        # Includes manual admissions and ready runs surviving process restart.
        # dispatching/unknown runs are never automatically replayed.
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT id,status FROM scheduled_runs WHERE status='ready' "
                "ORDER BY started_at,id LIMIT 20"
            ).fetchall()
            held = db.execute(
                "SELECT id,status FROM scheduled_runs WHERE status='awaiting_budget' "
                "AND json_extract(definition,'$.budgetAllowanceId') IS NOT NULL "
                "ORDER BY CASE WHEN id>? THEN 0 ELSE 1 END,id LIMIT 20",
                (self._budget_cursor,),
            ).fetchall()
            if held:
                self._budget_cursor = held[-1]["id"]
            held = list(held)
            selected = {row["id"] for row in held}
            # A run admitted after the cursor advanced can sort behind it. Include
            # every newly held run once in addition to the rotating old-hold batch.
            held.extend(
                {"id": identity, "status": "awaiting_budget"}
                for identity in sorted(newly_held - selected)
            )
            rows = list(rows) + held
        for row in rows:
            if self._stop.is_set():
                break
            if row["status"] == "awaiting_budget":
                try:
                    jobs.provision_budget(self.store, row["id"], self.adapter)
                except jobs.JobError:
                    # No effect was dispatched. Keep the run held; another run
                    # must not overtake it or obtain a newly invented budget ID.
                    continue
            jobs.dispatch_claim(self.store, {"id": row["id"]}, self.adapter)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # Do not log exception text: transport and input errors may hold
                # sensitive arguments. Durable run status remains authoritative.
                log.error("Scheduled worker tick failed; durable runs retained.")
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Scheduler worker was already started.")
        self._thread = threading.Thread(target=self._run, name="pi-scheduler", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            # Finish the bounded in-flight adapter before Store is closed.
            self._thread.join()
