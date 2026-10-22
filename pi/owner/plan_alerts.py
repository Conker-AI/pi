"""Plan usage alerts: a heads-up when a subscription plan passes the owner's level, and when it resets.

Readings come from the subscription runtimes as percentages of each plan window, never prices or
tokens. Pi keeps the latest reading per window and at most one alert per window period. Reading
and polling call no model, send nothing and spend nothing.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pi.core.errors import ServiceError
from pi.owner import preferences

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_usage (
    provider TEXT NOT NULL,
    window_id TEXT NOT NULL,
    label TEXT NOT NULL,
    used_percent INTEGER NOT NULL CHECK(used_percent BETWEEN 0 AND 100),
    resets_at TEXT,
    checked_at REAL NOT NULL,
    PRIMARY KEY (provider, window_id)
);
CREATE TABLE IF NOT EXISTS plan_alerts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    window_id TEXT NOT NULL,
    label TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    used_percent INTEGER NOT NULL,
    resets_at TEXT,
    raised_at REAL NOT NULL,
    reset_at REAL,
    dismissed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS plan_alerts_one_open
    ON plan_alerts(provider, window_id) WHERE reset_at IS NULL;
"""
NAMES = {"chatgpt": "ChatGPT", "claude": "Claude"}
JITTER_SECONDS = 60  # a reset time that moves less than this is the same period


class Reading(BaseModel):
    """What a runtime reports; parsed, not trusted to be well formed."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    # The runtimes' own label rule: words, spaces and . ( ) + / : - only.
    label: str = Field(pattern=r"^[\w .()+/:-]{1,60}$")
    usedPercent: int = Field(ge=0, le=100)
    resetsAt: str | None = Field(default=None, max_length=64)


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: Literal["chatgpt", "claude"]
    available: bool
    windows: list[Reading] = Field(default_factory=list, max_length=12)


class AlertError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def _moment(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def record(store, plan: dict, threshold: int, *, now: float | None = None) -> list[dict]:
    """Keep one plan's readings; raise or close alerts. Returns what changed."""
    now = time.time() if now is None else now
    plan = Plan.model_validate(plan)
    if not plan.available:
        return []
    changes = []
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for window in plan.windows:
            db.execute(
                "INSERT INTO plan_usage VALUES (?,?,?,?,?,?) ON CONFLICT(provider, window_id) "
                "DO UPDATE SET label=excluded.label, used_percent=excluded.used_percent, "
                "resets_at=excluded.resets_at, checked_at=excluded.checked_at",
                (plan.provider, window.id, window.label, window.usedPercent, window.resetsAt, now),
            )
            alert = db.execute(
                "SELECT * FROM plan_alerts WHERE provider=? AND window_id=? AND reset_at IS NULL",
                (plan.provider, window.id),
            ).fetchone()
            if alert is not None:
                ended, upcoming = _moment(alert["resets_at"]), _moment(window.resetsAt)
                over = (
                    (ended is not None and now >= ended)
                    or (
                        ended is not None
                        and upcoming is not None
                        and upcoming > ended + JITTER_SECONDS
                    )
                    or (ended is None and window.usedPercent < alert["threshold"])
                )
                if over:
                    db.execute("UPDATE plan_alerts SET reset_at=? WHERE id=?", (now, alert["id"]))
                    changes.append({"change": "reset", "alertId": alert["id"]})
                    alert = None
            if alert is None and window.usedPercent >= threshold:
                identity = "plan_" + uuid.uuid4().hex
                db.execute(
                    "INSERT INTO plan_alerts VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        identity,
                        plan.provider,
                        window.id,
                        window.label,
                        threshold,
                        window.usedPercent,
                        window.resetsAt,
                        now,
                    ),
                )
                changes.append({"change": "raised", "alertId": identity})
            elif alert is not None:
                db.execute(
                    "UPDATE plan_alerts SET used_percent=MAX(used_percent, ?) WHERE id=?",
                    (window.usedPercent, alert["id"]),
                )
        db.commit()
    return changes


def _title(row, reset: bool) -> str:
    name = f"{NAMES[row['provider']]} {row['label']}"
    if reset:
        return f"{name} limit reset"
    return f"{name} limit {row['used_percent']}% used"


def needs_you(store) -> list[dict]:
    """Open alerts the owner has not dismissed, for the Inbox."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM plan_alerts WHERE reset_at IS NULL AND dismissed_at IS NULL "
            "ORDER BY raised_at"
        ).fetchall()
    return [
        {
            "kind": "plan",
            "id": row["id"],
            "title": _title(row, False),
            "since": row["raised_at"],
            "usedPercent": row["used_percent"],
            "resetsAt": row["resets_at"],
            "source": {"kind": "plan-alert", "alertId": row["id"]},
        }
        for row in rows
    ]


def resets(store, since: float) -> list[dict]:
    """Alerts whose window reset since a moment (the owner's day), newest first."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM plan_alerts WHERE reset_at >= ? ORDER BY reset_at DESC LIMIT 50",
            (since,),
        ).fetchall()
    return [
        {
            "kind": "plan-reset",
            "title": _title(row, True),
            "at": row["reset_at"],
            "source": {"kind": "plan-alert", "alertId": row["id"]},
        }
        for row in rows
    ]


def overview(store) -> dict:
    """The latest reading of every plan window, and the alerts still open."""
    with store._connect() as db:
        usage = db.execute("SELECT * FROM plan_usage ORDER BY provider, window_id").fetchall()
    return {
        "windows": [
            {
                "provider": row["provider"],
                "window": row["window_id"],
                "label": row["label"],
                "usedPercent": row["used_percent"],
                "resetsAt": row["resets_at"],
                "checkedAt": row["checked_at"],
            }
            for row in usage
        ],
        "alerts": needs_you(store),
    }


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    """Hide an alert until its window resets; a later period can raise a new one."""
    with store._connect() as db:
        row = db.execute("SELECT * FROM plan_alerts WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise AlertError("not_found", "Plan alert not found.", 404)
        if row["dismissed_at"] is None:
            db.execute(
                "UPDATE plan_alerts SET dismissed_at=? WHERE id=?",
                (time.time() if now is None else now, identity),
            )
            db.commit()
    return {"id": identity, "dismissed": True}


class PlanAlertWorker:
    """Reads plan usage every few minutes while the owner's plan alerts are on."""

    def __init__(self, store, readers, *, check_seconds=900.0):
        if check_seconds <= 0:
            raise ValueError("The plan check interval must be positive.")
        self.store, self.readers, self.check_seconds = store, readers, check_seconds
        self._stop = threading.Event()
        self._thread = None

    def tick(self):
        settings = preferences.load(self.store)["preferences"]["planAlerts"]
        if not settings["enabled"]:
            return []
        changes = []
        for read in self.readers():
            try:
                changes.extend(record(self.store, read(), settings["percent"]))
            except Exception:
                log.warning("A plan usage reading failed; the next check retries.")
        return changes

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.error("Plan usage check failed.")
            self._stop.wait(self.check_seconds)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Plan alert worker was already started.")
        self._thread = threading.Thread(target=self._run, name="pi-plan-alerts", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
