"""Pi checks itself and tells the owner in the Inbox when a part of it stops working (R-E2).

Every few minutes the worker reads the same checks as `GET /health` (Pi's database, memory, the
local and hosted models, voice, ToolGate, the clean-up after deleted chats). A check that stays
`unavailable` or `degraded` for PERSIST_SECONDS raises one alert, an Inbox card saying what is
broken and since when; a blip shorter than that raises nothing. When the check works again the
alert closes and the Inbox's handled list says so. The owner may dismiss an alert; it stays
hidden until that check recovers and breaks again, and its recovery is not announced. After a
stop longer than STALE_SECONDS, a broken check starts its wait again. An `unknown` status (a
provider that is configured but not proven) is not an alert.

It covers what Pi can see from inside: if Pi itself is down, nothing here runs. The rows hold
check names, statuses and the checks' fixed reason strings, never a chat or a key.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from pi.core.errors import ServiceError

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS self_check_state (
    check_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    since REAL NOT NULL,
    checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS self_check_alerts (
    id TEXT PRIMARY KEY,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    raised_at REAL NOT NULL,
    cleared_at REAL,
    dismissed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS self_check_alerts_one_open
    ON self_check_alerts(check_name) WHERE cleared_at IS NULL;
"""
BROKEN = {"unavailable", "degraded"}
PERSIST_SECONDS = 900.0
# A gap this long since the last check means Pi was stopped; a check broken before the stop
# starts its 15 minutes again, so a restart does not raise an alert at once.
STALE_SECONDS = 1800.0
REASON_CHARS = 200
NAME_CHARS = 64
NAMES = {
    "store": "Pi's database",
    "memory": "Memory",
    "local_provider": "The local model",
    "hosted_provider": "OpenRouter",
    "provider_credentials": "Model provider keys",
    "speech": "Voice",
    "action_boundary": "Tools (ToolGate)",
    "forget_followups": "Clean-up after deleted chats",
}


class SelfCheckError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def _name(check: str) -> str:
    return NAMES.get(check, check.replace("_", " ").capitalize())


def _reason(check: dict) -> str:
    reason = check.get("reason")
    return reason[:REASON_CHARS] if isinstance(reason, str) else ""


def _one(db, name: str, check: dict, now: float) -> dict | None:
    status = str(check.get("status", "unknown"))[:32]
    broken = status in BROKEN
    state = db.execute("SELECT * FROM self_check_state WHERE check_name=?", (name,)).fetchone()
    # `since` is when the check last went from working to broken or back.
    fresh = state is not None and now - state["checked_at"] < STALE_SECONDS
    same = fresh and (state["status"] in BROKEN) == broken
    since = state["since"] if same else now
    db.execute(
        "INSERT INTO self_check_state VALUES (?,?,?,?) ON CONFLICT(check_name) "
        "DO UPDATE SET status=excluded.status, since=excluded.since, "
        "checked_at=excluded.checked_at",
        (name, status, since, now),
    )
    alert = db.execute(
        "SELECT * FROM self_check_alerts WHERE check_name=? AND cleared_at IS NULL", (name,)
    ).fetchone()
    if alert is not None and not broken:
        db.execute("UPDATE self_check_alerts SET cleared_at=? WHERE id=?", (now, alert["id"]))
        return {"change": "cleared", "alertId": alert["id"]}
    if alert is not None:
        db.execute(
            "UPDATE self_check_alerts SET status=?, reason=? WHERE id=?",
            (status, _reason(check), alert["id"]),
        )
        return None
    if broken and now - since >= PERSIST_SECONDS:
        identity = "sc_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO self_check_alerts VALUES (?,?,?,?,?,NULL,NULL)",
            (identity, name, status, _reason(check), since),
        )
        return {"change": "raised", "alertId": identity}
    return None


def _close_gone(db, seen: set, now: float) -> list[dict]:
    """A check that is no longer reported (renamed or removed by an update) closes its alert."""
    rows = db.execute(
        "SELECT id, check_name FROM self_check_alerts WHERE cleared_at IS NULL"
    ).fetchall()
    gone = [row["id"] for row in rows if row["check_name"] not in seen]
    for identity in gone:
        db.execute("UPDATE self_check_alerts SET cleared_at=? WHERE id=?", (now, identity))
    return [{"change": "cleared", "alertId": identity} for identity in gone]


def record(store, checks: dict, *, now: float | None = None) -> list[dict]:
    """Keep each check's state; raise, update or close alerts. Returns what changed."""
    now = time.time() if now is None else now
    changes = []
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            seen = set()
            for name, check in checks.items():
                if isinstance(name, str) and isinstance(check, dict):
                    seen.add(name[:NAME_CHARS])
                    change = _one(db, name[:NAME_CHARS], check, now)
                    if change is not None:
                        changes.append(change)
            if seen:
                changes.extend(_close_gone(db, seen, now))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return changes


def _source(row) -> dict:
    return {"kind": "self-check", "alertId": row["id"], "check": row["check_name"]}


def needs_you(store) -> list[dict]:
    """Open alerts the owner has not dismissed, for the Inbox."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM self_check_alerts WHERE cleared_at IS NULL AND dismissed_at IS NULL "
            "ORDER BY raised_at"
        ).fetchall()
    return [
        {
            "kind": "health",
            "id": row["id"],
            "title": f"{_name(row['check_name'])} is not working",
            "since": row["raised_at"],
            "status": row["status"],
            "reason": row["reason"] or None,
            "source": _source(row),
        }
        for row in rows
    ]


def recovered(store, since: float) -> list[dict]:
    """Alerts that closed since a moment (the owner's day), newest first."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM self_check_alerts WHERE cleared_at >= ? AND dismissed_at IS NULL "
            "ORDER BY cleared_at DESC LIMIT 50",
            (since,),
        ).fetchall()
    return [
        {
            "kind": "health-recovered",
            "title": f"{_name(row['check_name'])} works again",
            "at": row["cleared_at"],
            "source": _source(row),
        }
        for row in rows
    ]


def overview(store) -> dict:
    """The last state of every check and the alerts still open."""
    with store._connect() as db:
        states = db.execute("SELECT * FROM self_check_state ORDER BY check_name").fetchall()
    return {
        "checks": [
            {
                "check": row["check_name"],
                "name": _name(row["check_name"]),
                "status": row["status"],
                "since": row["since"],
                "checkedAt": row["checked_at"],
            }
            for row in states
        ],
        "alerts": needs_you(store),
    }


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    """Hide an open alert; the check raises a new one only after it recovers and breaks again."""
    with store._connect() as db:
        row = db.execute("SELECT * FROM self_check_alerts WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise SelfCheckError("not_found", "Alert not found.", 404)
        if row["dismissed_at"] is None:
            db.execute(
                "UPDATE self_check_alerts SET dismissed_at=? WHERE id=?",
                (time.time() if now is None else now, identity),
            )
            db.commit()
    return {"id": identity, "dismissed": True}


class SelfCheckWorker:
    """Reads Pi's health checks every few minutes and records them."""

    def __init__(self, store, probe, *, check_seconds: float = 300.0):
        if check_seconds <= 0:
            raise ValueError("The self-check interval must be positive.")
        self.store, self.probe, self.check_seconds = store, probe, check_seconds
        self._stop = threading.Event()
        self._thread = None

    def tick(self) -> list[dict]:
        return record(self.store, self.probe().get("checks", {}))

    def _run(self):
        # The first check waits one interval: services that start together are not broken.
        while not self._stop.wait(self.check_seconds):
            try:
                self.tick()
            except Exception:
                log.error("Pi's self-check failed; the next one retries.")

    def start(self):
        if self._thread is not None:
            raise RuntimeError("The self-check worker was already started.")
        self._thread = threading.Thread(target=self._run, name="pi-self-check", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
