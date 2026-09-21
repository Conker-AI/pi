"""Companion continuity from real events; reading never acknowledges or sends anything."""

import json
import time
from datetime import UTC, datetime

from pydantic import Field

from . import agents, owner_preferences, session_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_seen (
 event_id TEXT PRIMARY KEY REFERENCES activity_events(id), seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS continuity_order (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 activity_event_id TEXT UNIQUE REFERENCES activity_events(id),
 scheduled_run_id TEXT REFERENCES scheduled_runs(id),
 from_status TEXT, to_status TEXT, occurred_at REAL NOT NULL,
 provenance TEXT NOT NULL,
 CHECK ((activity_event_id IS NULL) != (scheduled_run_id IS NULL))
);
CREATE INDEX IF NOT EXISTS continuity_order_job ON continuity_order(scheduled_run_id,sequence);
CREATE TABLE IF NOT EXISTS continuity_ack (
 event_id TEXT PRIMARY KEY REFERENCES continuity_order(event_id), seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS continuity_delivered (
 event_id TEXT PRIMARY KEY REFERENCES continuity_order(event_id), delivered_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS continuity_migrations (id TEXT PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS continuity_activity AFTER INSERT ON activity_events
BEGIN
 INSERT INTO continuity_order
 (event_id,activity_event_id,from_status,to_status,occurred_at,provenance)
 VALUES(NEW.id,NEW.id,NEW.from_status,NEW.to_status,NEW.occurred_at,'recorded-event');
END;
CREATE TRIGGER IF NOT EXISTS continuity_job_status AFTER UPDATE OF status ON scheduled_runs
WHEN NEW.status != OLD.status
BEGIN
 INSERT INTO continuity_order
 (event_id,scheduled_run_id,from_status,to_status,occurred_at,provenance)
 VALUES('jobevt_' || lower(hex(randomblob(16))),NEW.id,OLD.status,NEW.status,
 (julianday('now')-2440587.5)*86400.0,'recorded-event');
END;
CREATE TRIGGER IF NOT EXISTS continuity_order_no_update BEFORE UPDATE ON continuity_order
BEGIN SELECT RAISE(ABORT,'continuity events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS continuity_order_no_delete BEFORE DELETE ON continuity_order
BEGIN SELECT RAISE(ABORT,'continuity events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS continuity_order_no_replace BEFORE INSERT ON continuity_order
WHEN EXISTS(SELECT 1 FROM continuity_order WHERE event_id=NEW.event_id OR sequence=NEW.sequence)
BEGIN SELECT RAISE(ABORT,'continuity events are append-only'); END;
"""
STATES = {
    "complete",
    "completed",
    "cancelled",
    "failed",
    "blocked",
    "awaiting_approval",
    "awaiting_budget",
    "acted_no_reply",
    "outcome_unknown",
    "interrupted",
}


def urgency(status):
    # An uncertain external effect needs reconciliation before safe retries.
    # Ordinary failures/approvals are attention-worthy, not automatically urgent.
    return {
        "urgent": status == "outcome_unknown",
        "basis": "uncertain-external-effect" if status == "outcome_unknown" else "status-update",
    }


class Acknowledge(agents.StrictModel):
    event_ids: list[str] = Field(min_length=1, max_length=100)


def initialize(db):
    """Preserve seen state; historical jobs are observations, not invented events."""
    # Install triggers and backfill under one writer lock: another process must
    # not allocate feed sequence 1 while historical sequence 1 is being copied.
    db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
    if not db.execute("SELECT 1 FROM continuity_migrations WHERE id='jobs-v1'").fetchone():
        db.execute("""INSERT INTO continuity_order
            (sequence,event_id,activity_event_id,from_status,to_status,occurred_at,provenance)
            SELECT sequence,id,id,from_status,to_status,occurred_at,'recorded-event'
            FROM activity_events e WHERE NOT EXISTS
            (SELECT 1 FROM continuity_order c WHERE c.activity_event_id=e.id) ORDER BY sequence""")
        db.execute(
            """INSERT INTO continuity_order
            (event_id,scheduled_run_id,to_status,occurred_at,provenance)
            SELECT 'jobevt_' || lower(hex(randomblob(16))),id,status,?,'state-observed'
            FROM scheduled_runs r WHERE NOT EXISTS
            (SELECT 1 FROM continuity_order c WHERE c.scheduled_run_id=r.id)
            ORDER BY started_at,id""",
            (time.time(),),
        )
        db.execute("INSERT INTO continuity_ack SELECT event_id,seen_at FROM continuity_seen")
        db.execute("INSERT INTO continuity_migrations VALUES ('jobs-v1')")
    db.commit()


def _item(db, entry):
    if entry["to_status"] not in STATES:
        return None
    common = {
        "eventId": entry["event_id"],
        "sequence": entry["sequence"],
        "status": entry["to_status"],
        "occurredAt": entry["occurred_at"],
        "provenance": entry["provenance"],
        "needsAttention": entry["to_status"] not in ("complete", "completed", "cancelled"),
        "urgency": urgency(entry["to_status"]),
    }
    if entry["scheduled_run_id"]:
        run = db.execute(
            "SELECT * FROM scheduled_runs WHERE id=?", (entry["scheduled_run_id"],)
        ).fetchone()
        if run is None:
            return None
        return {
            **common,
            "kind": "job_status",
            "title": json.loads(run["definition"])["name"],
            "sessionId": None,
            "taskId": None,
            "runId": None,
            "jobId": run["job_id"],
            "scheduledRunId": run["id"],
            "jobRevision": run["job_revision"],
            "source": {"kind": "scheduled-run", "jobId": run["job_id"], "runId": run["id"]},
        }
    event = db.execute(
        "SELECT * FROM activity_events WHERE id=?", (entry["activity_event_id"],)
    ).fetchone()
    if (
        event is None
        or event["kind"] not in ("task_status", "run_status")
        or not _eligible(db, event)
    ):
        return None
    session = db.execute("SELECT title FROM sessions WHERE id=?", (event["session_id"],)).fetchone()
    task = (
        db.execute("SELECT outcome FROM tasks WHERE id=?", (event["task_id"],)).fetchone()
        if event["task_id"]
        else None
    )
    return {
        **common,
        "kind": event["kind"],
        "title": task[0] if task else session[0] or "Conversation",
        "sessionId": event["session_id"],
        "taskId": event["task_id"],
        "runId": event["run_id"],
    }


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
        quiet = owner_preferences.quiet_now(prefs, now)
        # Select only the latest status per entity. Resolved blockers are not stale alerts.
        rows = db.execute(
            """SELECT c.* FROM continuity_order c
            LEFT JOIN activity_events e ON e.id=c.activity_event_id
            WHERE NOT EXISTS(SELECT 1 FROM continuity_ack s WHERE s.event_id=c.event_id)
            AND (
              (c.scheduled_run_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM continuity_order later WHERE later.sequence>c.sequence
                AND later.scheduled_run_id=c.scheduled_run_id))
              OR (e.kind IN ('run_status','task_status') AND NOT EXISTS(
                SELECT 1 FROM activity_events later WHERE later.sequence>e.sequence
                AND ((e.kind='run_status' AND later.run_id=e.run_id
                      AND later.kind IN ('run_status','run_started'))
                  OR (e.kind='task_status' AND later.task_id=e.task_id
                      AND later.kind='task_status')))))
            AND (? IS NULL OR c.sequence<?) ORDER BY c.sequence DESC LIMIT 1000""",
            (before, before),
        ).fetchall()
        items = []
        suppression = {}
        examined = None
        for entry in rows:
            examined = entry["sequence"]
            item = _item(db, entry)
            if item is None:
                continue
            suppressed = []
            urgent = item["urgency"]["urgent"]
            if prefs.urgency == "off":
                suppressed.append("proactivity_off")
            elif prefs.urgency == "urgent_only" and not urgent:
                suppressed.append("urgent_only")
            if quiet and not (urgent and prefs.quietHours.urgentExceptions):
                suppressed.append("quiet_hours")
            suppression[item["eventId"]] = suppressed
            items.append(item)
            if len(items) == limit:
                break
        next_cursor = (
            examined
            if rows
            and (len(rows) == 1000 or (examined is not None and examined != rows[-1]["sequence"]))
            else None
        )
        if items:
            reasons = sorted({reason for values in suppression.values() for reason in values})
        elif prefs.urgency == "off":
            reasons = ["proactivity_off"]
        elif quiet:
            reasons = ["quiet_hours"]
        return {
            "items": items,
            "nextCursor": next_cursor,
            "preferenceRevision": row["revision"],
            "notificationSuppressed": bool(reasons)
            and not any(not values for values in suppression.values()),
            "notificationSuppressionByEvent": suppression,
            "suppressionReasons": reasons,
            "notificationDelivery": "owner-poll; explicit delivery acknowledgement",
            "summaryGeneration": "recorded-status-summary; no model",
            "summary": summarize(items, more=next_cursor is not None),
            "readMarksSeen": False,
        }


def summarize(items, *, more=False):
    """Summarize visible status updates, never infer task completion or total history."""
    finished = sum(item["status"] in ("complete", "completed") for item in items)
    attention = sum(item["needsAttention"] for item in items)
    cancelled = sum(item["status"] == "cancelled" for item in items)
    if not items:
        text = "No unseen work updates on this page." if more else "No unseen work updates."
    else:
        text = f"{len(items)} unseen work update{'s' if len(items) != 1 else ''}: "
        text += f"{finished} completed, {attention} needing attention."
        if cancelled:
            text += f" {cancelled} cancelled."
        if more:
            text += " More updates are available."
    return {
        "text": text,
        "scope": "returned-page",
        "hasMore": more,
        "completedUpdates": finished,
        "attentionUpdates": attention,
        "cancelledUpdates": cancelled,
        "eventIds": [item["eventId"] for item in items],
        "highlights": [
            {
                "eventId": item["eventId"],
                "title": item["title"],
                "status": item["status"],
                "kind": item["kind"],
                "sessionId": item.get("sessionId"),
                "runId": item.get("runId"),
                "taskId": item.get("taskId"),
                "jobId": item.get("jobId"),
                "scheduledRunId": item.get("scheduledRunId"),
            }
            for item in sorted(items, key=lambda item: not item["needsAttention"])[:5]
        ],
    }


def acknowledge(store, body):
    body = Acknowledge.model_validate(body.model_dump())
    ids = agents.references(body.event_ids)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for identity in ids:
            row = db.execute(
                "SELECT * FROM continuity_order WHERE event_id=?", (identity,)
            ).fetchone()
            if row is None or _item(db, row) is None:
                raise agents.AgentError(
                    "event_unavailable", "A selected continuity event is unavailable.", 404
                )
        for identity in ids:
            db.execute("INSERT OR IGNORE INTO continuity_ack VALUES (?,?)", (identity, time.time()))
        db.commit()
    return {"acknowledged": ids}


def notifications(store, *, limit=30, before=None, now=None):
    """At-least-once owner delivery; clients deduplicate by stable eventId.

    Delivery acknowledgement means shown, not read. Suppressed/private events
    are not consumed, and no external push service or model is invoked.
    """
    feed = briefing(store, limit=limit, before=before, now=now)
    identities = [item["eventId"] for item in feed["items"]]
    with store._connect() as db:
        delivered = (
            {
                row[0]
                for row in db.execute(
                    "SELECT event_id FROM continuity_delivered WHERE event_id IN ("
                    + ",".join("?" for _ in identities)
                    + ")",
                    identities,
                )
            }
            if identities
            else set()
        )
    items = [
        item
        for item in feed["items"]
        if item["eventId"] not in delivered
        and not feed["notificationSuppressionByEvent"][item["eventId"]]
    ]
    return {
        "items": items,
        "nextCursor": feed["nextCursor"],
        "suppressionReasons": feed["suppressionReasons"],
        "channel": "owner-poll",
        "delivery": "at-least-once",
        "deduplicationKey": "eventId",
        "readMarksSeen": False,
    }


def delivered(store, body):
    body = Acknowledge.model_validate(body.model_dump())
    ids = agents.references(body.event_ids)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for identity in ids:
            entry = db.execute(
                "SELECT * FROM continuity_order WHERE event_id=?", (identity,)
            ).fetchone()
            if entry is None or _item(db, entry) is None:
                raise agents.AgentError("event_unavailable", "Notification is unavailable.", 404)
        for identity in ids:
            db.execute(
                "INSERT OR IGNORE INTO continuity_delivered VALUES (?,?)", (identity, time.time())
            )
        db.commit()
    return {"delivered": ids}
