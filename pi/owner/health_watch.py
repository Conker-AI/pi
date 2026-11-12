"""Health watch: one Inbox card when a part of Conker stops working, closed when it works again.

The owner should hear about a broken part from Conker, not find it himself. Every 15 minutes Pi
asks each part whether it works: ToolGate, MemoryGate, the answer model's provider, web search and
memory saving. Checks read health signals only: no model call, no search query, nothing spent.
Two failures in a row file one card (one blip is not a card); a check that works again closes its
card, which then shows as handled. The probes are passed in, so this module knows no transport.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime

from pi.core.errors import ServiceError
from pi.owner import settings as owner_settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS health_checks (
    check_id TEXT PRIMARY KEY,
    failures INTEGER NOT NULL CHECK(failures >= 0),
    reason TEXT,
    checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS health_alerts (
    id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    raised_at REAL NOT NULL,
    fixed_at REAL,
    dismissed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS health_alerts_one_open
    ON health_alerts(check_id) WHERE fixed_at IS NULL;
"""
CHECK_SECONDS = 900.0
FAILURES_TO_FILE = 2
# Settings, Server (Conker's settings registry): the owner can turn the watch off; on by default.
SETTING = "server.health_watch"
# What the owner calls each part; a card's reason is a sentence the probe wrote.
NAMES = {
    "toolgate": "Tools and actions",
    "memorygate": "Long-term memory",
    "memory_delivery": "Saving to memory",
    "answer_model": "The answer model",
    "web_search": "Web search",
}
MAX_REASON = 200

# A probe answers None when it cannot tell (the part is not set up, or a check it depends on is
# down), (True, "") when the part works, and (False, reason) when it does not.
Result = tuple[bool, str] | None
WORKS: Result = (True, "")


def broken(reason: str) -> Result:
    return (False, reason)


class HealthWatchError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def enabled(store) -> bool:
    """On unless the owner turned it off; any value but false keeps it on."""
    return owner_settings.value(store, SETTING, True) is not False


def record(store, check_id: str, result: Result, *, now: float | None = None) -> dict | None:
    """Keep one check's outcome; file or close its card. Returns the change, if any."""
    if check_id not in NAMES:
        raise ValueError(f"Unknown health check {check_id!r}.")
    if result is None:
        return None
    now = time.time() if now is None else now
    works, reason = result
    reason = " ".join(str(reason).split())[:MAX_REASON] or "It does not answer."
    change = None
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT failures FROM health_checks WHERE check_id=?", (check_id,)
        ).fetchone()
        failures = 0 if works else (previous["failures"] if previous else 0) + 1
        db.execute(
            "INSERT INTO health_checks VALUES (?,?,?,?) ON CONFLICT(check_id) DO UPDATE SET "
            "failures=excluded.failures, reason=excluded.reason, checked_at=excluded.checked_at",
            (check_id, failures, None if works else reason, now),
        )
        alert = db.execute(
            "SELECT id FROM health_alerts WHERE check_id=? AND fixed_at IS NULL", (check_id,)
        ).fetchone()
        if works and alert is not None:
            db.execute("UPDATE health_alerts SET fixed_at=? WHERE id=?", (now, alert["id"]))
            change = {"change": "fixed", "alertId": alert["id"], "check": check_id}
        elif not works and alert is not None:
            db.execute("UPDATE health_alerts SET reason=? WHERE id=?", (reason, alert["id"]))
        elif not works and failures >= FAILURES_TO_FILE:
            identity = "health_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO health_alerts VALUES (?,?,?,?,NULL,NULL)",
                (identity, check_id, reason, now),
            )
            change = {"change": "raised", "alertId": identity, "check": check_id}
        db.commit()
    return change


def _source(row) -> dict:
    return {"kind": "health-alert", "alertId": row["id"], "check": row["check_id"]}


def needs_you(store) -> list[dict]:
    """Open cards the owner has not dismissed, shaped as the Inbox's attention items."""
    if not enabled(store):
        return []
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM health_alerts WHERE fixed_at IS NULL AND dismissed_at IS NULL "
            "ORDER BY raised_at"
        ).fetchall()
    return [
        {
            "kind": "attention",
            "id": row["id"],
            "title": row["reason"],
            "since": row["raised_at"],
            "source": _source(row),
            "urgency": {"urgent": False, "basis": "health-check"},
            "status": "failed",
        }
        for row in rows
    ]


def fixes(store, since: float, zone) -> list[dict]:
    """Cards closed since a moment (the owner's day), newest first, for Handled."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM health_alerts WHERE fixed_at >= ? ORDER BY fixed_at DESC LIMIT 50",
            (since,),
        ).fetchall()
    return [
        {
            "kind": "attention",
            "title": f"{NAMES.get(row['check_id'], 'A part of Conker')} works again "
            f"(fixed at {datetime.fromtimestamp(row['fixed_at'], zone):%H:%M})",
            "at": row["fixed_at"],
            "source": _source(row),
        }
        for row in rows
    ]


def overview(store) -> dict:
    """Each check's latest outcome and the cards still open."""
    with store._connect() as db:
        rows = db.execute("SELECT * FROM health_checks ORDER BY check_id").fetchall()
    return {
        "enabled": enabled(store),
        "checks": [
            {
                "check": row["check_id"],
                "name": NAMES.get(row["check_id"], row["check_id"]),
                "works": row["failures"] == 0,
                "failures": row["failures"],
                "reason": row["reason"],
                "checkedAt": row["checked_at"],
            }
            for row in rows
        ],
        "alerts": needs_you(store),
    }


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    """Hide a card until its check works again; a later failure can file a new one."""
    with store._connect() as db:
        row = db.execute(
            "SELECT dismissed_at FROM health_alerts WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            raise HealthWatchError("not_found", "Health card not found.", 404)
        if row["dismissed_at"] is None:
            db.execute(
                "UPDATE health_alerts SET dismissed_at=? WHERE id=?",
                (time.time() if now is None else now, identity),
            )
            db.commit()
    return {"id": identity, "dismissed": True}


class HealthWatchWorker:
    """Runs every probe every 15 minutes while the owner's watch is on."""

    def __init__(
        self, store, probes: Callable[[], dict[str, Callable[[], Result]]], *, check_seconds=None
    ):
        check_seconds = CHECK_SECONDS if check_seconds is None else check_seconds
        if check_seconds <= 0:
            raise ValueError("The health check interval must be positive.")
        self.store, self.probes, self.check_seconds = store, probes, check_seconds
        self._stop = threading.Event()
        self._thread = None

    def tick(self) -> list[dict]:
        """One round of checks. Never raises: a failing probe or write is logged and retried."""
        try:
            if not enabled(self.store):
                return []
            probes = self.probes()
        except Exception:
            log.error("The health watch could not start a round; the next check retries.")
            return []
        changes = []
        for check_id, probe in probes.items():
            try:
                change = record(self.store, check_id, probe())
            except Exception:
                log.warning("The %s health check failed to run; the next check retries.", check_id)
                continue
            if change is not None:
                changes.append(change)
        return changes

    def _run(self):
        # The first round waits one interval: the other services may still be starting.
        while not self._stop.wait(self.check_seconds):
            self.tick()

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Health watch worker was already started.")
        self._thread = threading.Thread(target=self._run, name="pi-health-watch", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
