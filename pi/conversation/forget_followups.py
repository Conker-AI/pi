"""What Pi still owes other services after the owner forgot a chat, done once Pi runs again.

Forgetting runs with Pi stopped (`pi.conversation.forgetting`), so it only queues two things:
the ToolGate approval requests the chat's parked turns were waiting on, to withdraw
(`pi.actions.withdrawals`), and the memories Conker kept from the chat on its own, to forget in
MemoryGate (`pi.memory.followups`). This worker does both every `interval` seconds, retries what
failed with a growing wait, and `status` says what is still owed (Pi's health, `forget_followups`).
Nothing here can undo the forgetting itself, and no row holds any text from the chat.
"""

from __future__ import annotations

import logging
import threading

from pi.actions import withdrawals
from pi.memory import followups as memory_followups

log = logging.getLogger(__name__)


def tick(store, toolgate, corrections, now=None) -> int:
    return withdrawals.drain(store, toolgate, now=now) + memory_followups.drain(
        store, corrections, now=now
    )


def status(store, *, toolgate=True, memory=True) -> dict:
    """Counts only. Degraded while something is blocked, or waits on a service not set up."""
    with store._connect() as db:
        approvals = dict(
            db.execute(
                "SELECT state, COUNT(*) FROM approval_withdrawals "
                "WHERE state IN ('pending','blocked') GROUP BY state"
            ).fetchall()
        )
        memories = dict(
            db.execute(
                "SELECT undo, COUNT(*) FROM keep_results WHERE kind='memory' "
                "AND undo IN ('pending','blocked') GROUP BY undo"
            ).fetchall()
        )
    blocked = approvals.get("blocked", 0) + memories.get("blocked", 0)
    stranded = (not toolgate and sum(approvals.values())) or (not memory and sum(memories.values()))
    result = {
        "status": "degraded" if blocked or stranded else "ok",
        "approvalWithdrawals": sum(approvals.values()),
        "memoryForgets": sum(memories.values()),
        "blocked": blocked,
    }
    if blocked or stranded:
        result["reason"] = (
            "Something a forgotten chat left in ToolGate or MemoryGate cannot be removed yet; "
            "Pi keeps trying."
        )
    return result


class Followups:
    def __init__(self, store, toolgate, corrections, interval=30.0):
        if interval <= 0:
            raise ValueError("Follow-up interval must be positive.")
        self.store, self.toolgate, self.corrections = store, toolgate, corrections
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def status(self) -> dict:
        return status(
            self.store, toolgate=self.toolgate is not None, memory=self.corrections is not None
        )

    def _run(self):
        while not self._stop.is_set():
            try:
                tick(self.store, self.toolgate, self.corrections)
            except Exception:
                # Content-free: the rows hold ids only and stay queued.
                log.error("Forgetting follow-ups paused; they stay queued and run again.")
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="pi-forget-followups", daemon=True
            )
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
