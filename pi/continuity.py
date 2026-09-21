"""Companion continuity from real events; reading never acknowledges or sends anything."""

import time
from datetime import UTC, datetime

from pydantic import Field

from . import agents, owner_preferences, session_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_seen (
 event_id TEXT PRIMARY KEY REFERENCES activity_events(id), seen_at REAL NOT NULL
);
"""
STATES = {
    "complete",
    "completed",
    "failed",
    "blocked",
    "awaiting_approval",
    "awaiting_budget",
    "acted_no_reply",
    "outcome_unknown",
    "interrupted",
}


class Acknowledge(agents.StrictModel):
    event_ids: list[str] = Field(min_length=1, max_length=100)


def _eligible(db, row):
    privacy = session_settings.source_privacy(db, row["session_id"])
    return privacy is not None and not any(privacy.values())


def briefing(store, *, limit=30, before=None, now=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Choose 1-100 results.")
    if before is not None and (type(before) is not int or before < 1):
        raise ValueError("Use the returned event cursor.")
    now = now or datetime.now(UTC)
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute(
            "SELECT revision,preferences FROM owner_preferences WHERE singleton=1"
        ).fetchone()
        prefs = owner_preferences.OwnerPreferences.model_validate_json(row["preferences"])
        reasons = []
        if prefs.urgency != "meaningful":
            reasons.append(
                "proactivity_off" if prefs.urgency == "off" else "no_urgent_event_classification"
            )
        if owner_preferences.quiet_now(prefs, now):
            reasons.append("quiet_hours")
        # Select only the latest status per entity. Resolved blockers are not stale alerts.
        rows = db.execute(
            """SELECT e.* FROM activity_events e
            WHERE e.kind IN ('run_status','task_status')
            AND NOT EXISTS(SELECT 1 FROM continuity_seen s WHERE s.event_id=e.id)
            AND NOT EXISTS(SELECT 1 FROM activity_events later WHERE later.sequence>e.sequence
                AND later.kind IN ('run_status','run_started','task_status')
                AND ((e.kind='run_status' AND later.run_id=e.run_id
                      AND later.kind IN ('run_status','run_started'))
                  OR (e.kind='task_status' AND later.task_id=e.task_id
                      AND later.kind='task_status')))
            AND (? IS NULL OR e.sequence<?) ORDER BY e.sequence DESC LIMIT 1000""",
            (before, before),
        ).fetchall()
        items = []
        examined = None
        for event in rows:
            examined = event["sequence"]
            if event["to_status"] not in STATES or not _eligible(db, event):
                continue
            session = db.execute(
                "SELECT title FROM sessions WHERE id=?", (event["session_id"],)
            ).fetchone()
            task = (
                db.execute("SELECT outcome FROM tasks WHERE id=?", (event["task_id"],)).fetchone()
                if event["task_id"]
                else None
            )
            items.append(
                {
                    "eventId": event["id"],
                    "sequence": event["sequence"],
                    "status": event["to_status"],
                    "kind": event["kind"],
                    "title": task[0] if task else session[0] or "Conversation",
                    "sessionId": event["session_id"],
                    "taskId": event["task_id"],
                    "runId": event["run_id"],
                    "occurredAt": event["occurred_at"],
                    "provenance": "recorded-event",
                    "needsAttention": event["to_status"] not in ("complete", "completed"),
                }
            )
            if len(items) == limit:
                break
        return {
            "items": items,
            "nextCursor": examined
            if rows
            and (len(rows) == 1000 or (examined is not None and examined != rows[-1]["sequence"]))
            else None,
            "preferenceRevision": row["revision"],
            "notificationSuppressed": bool(reasons),
            "suppressionReasons": reasons,
            "notificationDelivery": "not-configured",
            "summaryGeneration": "none",
            "readMarksSeen": False,
        }


def acknowledge(store, body):
    body = Acknowledge.model_validate(body.model_dump())
    ids = agents.references(body.event_ids)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for identity in ids:
            row = db.execute("SELECT * FROM activity_events WHERE id=?", (identity,)).fetchone()
            if (
                row is None
                or row["kind"] not in ("run_status", "task_status")
                or row["to_status"] not in STATES
                or not _eligible(db, row)
            ):
                raise agents.AgentError(
                    "event_unavailable", "A selected continuity event is unavailable.", 404
                )
        for identity in ids:
            db.execute(
                "INSERT OR IGNORE INTO continuity_seen VALUES (?,?)", (identity, time.time())
            )
        db.commit()
    return {"acknowledged": ids}
