"""Atomic owner-policy admission; separate from ToolGate spending authority."""

import json
from datetime import UTC, datetime

from pydantic import Field

from . import owner_preferences as prefs

SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_reservations (
 request_id TEXT PRIMARY KEY, preference_revision INTEGER NOT NULL,
 proposed TEXT NOT NULL, urgent INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS proactive_reservations_no_update
BEFORE UPDATE ON proactive_reservations
BEGIN SELECT RAISE(ABORT,'proactive reservations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS proactive_reservations_no_delete
BEFORE DELETE ON proactive_reservations
BEGIN SELECT RAISE(ABORT,'proactive reservations are permanent'); END;
CREATE TRIGGER IF NOT EXISTS proactive_reservations_no_replace
BEFORE INSERT ON proactive_reservations
WHEN EXISTS(SELECT 1 FROM proactive_reservations WHERE request_id=NEW.request_id)
BEGIN SELECT RAISE(ABORT,'proactive reservation exists'); END;
"""


class Request(prefs.StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    expected_revision: int = Field(ge=1)
    proposed: prefs.Usage
    urgent: bool = False


class Denied(ValueError):
    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("Proactive work was not admitted.")


def _usage(db, preferences, now):
    day = prefs.local_day(preferences, now)
    used = {key: 0 for key in ("suggestions", "researchMinutes", "costCents")}
    # Reclassify reservations in the currently selected zone: changing time zone
    # cannot reset usage already incurred on that local day.
    for row in db.execute(
        "SELECT proposed,created_at FROM proactive_reservations WHERE created_at>=?",
        (now.timestamp() - 3 * 86400,),
    ):
        if prefs.local_day(preferences, datetime.fromtimestamp(row["created_at"], UTC)) == day:
            for key, value in json.loads(row["proposed"]).items():
                used[key] += value
    return day, used


def status(store, *, now=None):
    now = now or datetime.now(UTC)
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM owner_preferences WHERE singleton=1").fetchone()
        preferences = prefs.OwnerPreferences.model_validate_json(row["preferences"])
        day, used = _usage(db, preferences, now)
        return {
            "day": day,
            "timeZone": preferences.quietHours.timeZone,
            "preferenceRevision": row["revision"],
            "reserved": used,
            "limits": preferences.dailyBudget.model_dump(),
            "grantsExecutionAuthority": False,
        }


def reserve(store, body, *, now=None):
    body = Request.model_validate(body.model_dump())
    now = now or datetime.now(UTC)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM owner_preferences WHERE singleton=1").fetchone()
        preferences = prefs.OwnerPreferences.model_validate_json(row["preferences"])
        day, used = _usage(db, preferences, now)
        existing = db.execute(
            "SELECT * FROM proactive_reservations WHERE request_id=?", (body.request_id,)
        ).fetchone()
        proposed = body.proposed.model_dump()
        if existing:
            if (
                json.loads(existing["proposed"]) != proposed
                or existing["urgent"] != body.urgent
                or existing["preference_revision"] != body.expected_revision
            ):
                raise Denied(["request_identity_conflict"])
            return {
                "requestId": body.request_id,
                "replayed": True,
                "grantsExecutionAuthority": False,
            }
        if row["revision"] != body.expected_revision:
            raise Denied(["preferences_changed"])
        reasons = prefs.admission_reasons(
            preferences,
            now,
            urgent=body.urgent,
            usage_day=day,
            used=prefs.Usage(**used),
            proposed=body.proposed,
        )
        if reasons:
            raise Denied(reasons)
        db.execute(
            "INSERT INTO proactive_reservations VALUES (?,?,?,?,?)",
            (
                body.request_id,
                row["revision"],
                json.dumps(proposed),
                int(body.urgent),
                now.timestamp(),
            ),
        )
        db.commit()
        return {"requestId": body.request_id, "replayed": False, "grantsExecutionAuthority": False}
