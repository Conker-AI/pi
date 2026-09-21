"""Opt-in FIFO queue draining; all admission and replay checks remain durable."""

import logging
import threading

from . import turn_queue

log = logging.getLogger(__name__)


class QueueWorker:
    def __init__(self, store, loop, interval=1.0):
        if interval <= 0:
            raise ValueError("Queue interval must be positive.")
        self.store, self.loop, self.interval = store, loop, interval
        self._stop = threading.Event()
        self._thread = None
        self._cursor = ""

    def tick(self):
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT q.session_id FROM conversation_queues q JOIN sessions s "
                "ON s.id=q.session_id WHERE q.paused=0 AND s.status='open' AND EXISTS "
                "(SELECT 1 FROM queued_turns e WHERE e.session_id=q.session_id "
                "AND e.state IN ('waiting','claimed')) "
                "ORDER BY (q.session_id<=?),q.session_id LIMIT 20",
                (self._cursor,),
            ).fetchall()
        for row in rows:
            if self._stop.is_set():
                break
            self._cursor = row[0]
            try:
                turn_queue.run_next(self.store, self.loop, row[0])
            except Exception:
                log.error("Queue tick failed; durable requests retained.")

    def _run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Queue worker already started.")
        self._thread = threading.Thread(target=self._run, name="pi-turn-queue", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
