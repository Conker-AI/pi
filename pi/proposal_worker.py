"""Opt-in timer for proposal passes; the pass itself enforces budget and privacy."""

from __future__ import annotations

import logging
import threading

from . import proposals

log = logging.getLogger(__name__)


class ProposalWorker:
    def __init__(self, store, router, *, interval_hours=24.0, check_seconds=3600.0):
        if interval_hours <= 0 or check_seconds <= 0:
            raise ValueError("Proposal intervals must be positive.")
        self.store, self.router = store, router
        self.interval_seconds, self.check_seconds = interval_hours * 3600, check_seconds
        self._stop = threading.Event()
        self._thread = None

    def tick(self):
        if not proposals.due(self.store, self.interval_seconds):
            return None
        enabled, complete = proposals.model_call(self.store, self.router)
        return proposals.run_pass(self.store, complete, role_enabled=enabled)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # Exception text may hold conversation content; the pass record is authoritative.
                log.error("Proposal pass failed; see the latest pass record.")
            self._stop.wait(self.check_seconds)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Proposal worker was already started.")
        self._thread = threading.Thread(target=self._run, name="pi-proposals", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
