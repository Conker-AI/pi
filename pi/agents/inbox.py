"""Inbox: everything that happened and everything that needs the owner."""

import json
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pi.agents import continuity, workflow_offers, workflow_proposals
from pi.conversation import settings as session_settings
from pi.owner import plan_alerts
from pi.owner import preferences as owner_preferences
from pi.schedule import agent_checks


def _owner_zone(store):
    """The owner's time zone and its name; UTC when no preferences are saved."""
    with store._connect() as db:
        row = db.execute("SELECT preferences FROM owner_preferences WHERE singleton=1").fetchone()
    if not row:
        return UTC, "UTC"
    name = owner_preferences.OwnerPreferences.model_validate_json(row["preferences"]).quietHours
    return ZoneInfo(name.timeZone), name.timeZone


def _local_midnight(day, zone):
    return datetime.combine(day, datetime.min.time(), zone).timestamp()


def _needs_approval_items(store, day_start):
    """Get items needing approval from turns."""
    items = []
    for turn in store.awaiting_approval():
        with store._connect() as db:
            privacy = session_settings.source_privacy(db, turn["session_id"])
            is_private = privacy is not None and any(privacy.values())

            forgotten = db.execute(
                "SELECT 1 FROM forgotten_sessions WHERE session_id=?", (turn["session_id"],)
            ).fetchone()

            if forgotten:
                continue

            title = (
                "Private chat"
                if is_private
                else db.execute(
                    "SELECT title FROM sessions WHERE id=?", (turn["session_id"],)
                ).fetchone()[0]
                or "Conversation"
            )

            items.append(
                {
                    "kind": "approval",
                    "id": turn["id"],
                    "title": title,
                    "tool": turn["approval_tool_id"],
                    "since": turn["started_at"],
                    "source": {
                        "kind": "turn",
                        "sessionId": turn["session_id"],
                        "turnId": turn["id"],
                    },
                }
            )
    return items


def _needs_suggestion_items(store):
    """Get open companion proposals."""
    items = []
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM proposals WHERE state='open' ORDER BY created_at"
        ).fetchall()

        for row in rows:
            items.append(
                {
                    "kind": "suggestion",
                    "id": row["id"],
                    "title": row["title"],
                    "since": row["created_at"],
                    "source": {"kind": "proposal", "proposalId": row["id"]},
                }
            )
    return items


def _needs_memory_items(store):
    """Get pending memory proposals."""
    items = []
    with store._connect() as db:
        rows = db.execute(
            """SELECT mp.*, s.title as session_title FROM memory_proposals mp
               LEFT JOIN sessions s ON s.id=mp.session_id
               WHERE mp.state='pending'
               AND mp.session_id NOT IN (SELECT session_id FROM forgotten_sessions)
               ORDER BY mp.created_at"""
        ).fetchall()

        for row in rows:
            items.append(
                {
                    "kind": "memory",
                    "id": row["id"],
                    "title": "Memory change to review",
                    "since": row["created_at"],
                    "source": {
                        "kind": "memory-proposal",
                        "proposalId": row["id"],
                        "sessionId": row["session_id"],
                    },
                }
            )
    return items


def _needs_attention_items(store):
    """Get continuity items needing attention."""
    items = []
    briefing = continuity.briefing(store, limit=100)

    approval_turn_ids = set()
    for turn in store.awaiting_approval():
        approval_turn_ids.add(turn["id"])

    for item in briefing["items"]:
        if not item["needsAttention"]:
            continue

        if item.get("runId") in approval_turn_ids:
            continue

        items.append(
            {
                "kind": "attention",
                "id": item["eventId"],
                "title": item["title"],
                "since": item["occurredAt"],
                "source": item.get("source"),
                "urgency": item["urgency"],
                "status": item["status"],
            }
        )
    return items


def _handled_items(store, day_start):
    """Get completed work since the start of the owner's day."""
    items = []
    with store._connect() as db:
        rows = db.execute(
            """SELECT co.*, ae.session_id, ae.kind as event_kind, ae.task_id, ae.run_id
               FROM continuity_order co
               LEFT JOIN activity_events ae ON ae.id=co.activity_event_id
               WHERE co.occurred_at >= ? AND co.to_status IN ('complete', 'completed')
               ORDER BY co.occurred_at DESC LIMIT 50""",
            (day_start,),
        ).fetchall()

        for row in rows:
            if row["scheduled_run_id"]:
                run = db.execute(
                    "SELECT * FROM scheduled_runs WHERE id=?", (row["scheduled_run_id"],)
                ).fetchone()
                if run:
                    definition = json.loads(run["definition"])
                    items.append(
                        {
                            "kind": "job",
                            "title": definition["name"],
                            "at": row["occurred_at"],
                            "source": {
                                "kind": "scheduled-run",
                                "jobId": run["job_id"],
                                "runId": run["id"],
                            },
                        }
                    )
            elif row["activity_event_id"] and row["event_kind"] in ("task_status", "run_status"):
                event = db.execute(
                    "SELECT * FROM activity_events WHERE id=?", (row["activity_event_id"],)
                ).fetchone()
                if event and continuity._eligible(db, event):
                    session = db.execute(
                        "SELECT title FROM sessions WHERE id=?", (event["session_id"],)
                    ).fetchone()
                    task = None
                    if event["task_id"]:
                        task = db.execute(
                            "SELECT outcome FROM tasks WHERE id=?", (event["task_id"],)
                        ).fetchone()

                    title = task[0] if task else (session[0] if session else "Conversation")
                    kind = "task" if row["event_kind"] == "task_status" else "run"

                    # For run_status, get the actual turn end time
                    actual_time = row["occurred_at"]
                    if event["run_id"]:
                        turn = db.execute(
                            "SELECT ended_at FROM turns WHERE id=?", (event["run_id"],)
                        ).fetchone()
                        if turn and turn["ended_at"]:
                            actual_time = turn["ended_at"]

                    items.append(
                        {
                            "kind": kind,
                            "title": title,
                            "at": actual_time,
                            "source": {"kind": "activity-event", "eventId": row["event_id"]},
                        }
                    )
    return items


def _upcoming_items(store, now):
    """Get upcoming scheduled jobs and open tasks."""
    items = []
    week_later = now + (7 * 24 * 60 * 60)

    with store._connect() as db:
        rows = db.execute(
            """SELECT * FROM scheduled_jobs
               WHERE next_at <= ? AND next_at > ?
               ORDER BY next_at ASC LIMIT 20""",
            (week_later, now),
        ).fetchall()

        for row in rows:
            definition = json.loads(row["definition"])
            items.append(
                {
                    "kind": "job",
                    "id": row["id"],
                    "title": definition["name"],
                    "at": row["next_at"],
                    "source": {"kind": "scheduled-job", "jobId": row["id"]},
                }
            )

        # Open tasks (planned, in_progress, not archived, chat not forgotten or private)
        task_rows = db.execute(
            """SELECT t.*, s.title as session_title
               FROM tasks t
               JOIN sessions s ON s.id=t.session_id
               WHERE t.status IN ('planned', 'in_progress')
               AND t.session_id NOT IN (SELECT session_id FROM forgotten_sessions)
               AND t.archived_at IS NULL
               ORDER BY t.created_at DESC LIMIT 20"""
        ).fetchall()

        for row in task_rows:
            privacy = session_settings.source_privacy(db, row["session_id"])
            if privacy is not None and any(privacy.values()):
                continue

            items.append(
                {
                    "kind": "task",
                    "id": row["id"],
                    "title": row["outcome"],
                    "at": None,
                    "source": {"kind": "task", "taskId": row["id"]},
                }
            )

    return items


def _impact_counts(store, day_start, week_start):
    """Calculate impact counts for today and this week."""

    def _count_period(start_time):
        with store._connect() as db:
            answers = db.execute(
                """SELECT COUNT(*) FROM turns t
                   JOIN sessions s ON s.id=t.session_id
                   WHERE t.status='complete' AND t.ended_at >= ?
                   AND t.session_id NOT IN (SELECT session_id FROM forgotten_sessions)""",
                (start_time,),
            ).fetchone()[0]

            actions = db.execute(
                """SELECT COUNT(*) FROM tool_actions ta
                   JOIN turns t ON t.id=ta.turn_id
                   JOIN sessions s ON s.id=t.session_id
                   WHERE ta.state='completed' AND ta.created_at >= ?
                   AND t.session_id NOT IN (SELECT session_id FROM forgotten_sessions)""",
                (start_time,),
            ).fetchone()[0]

            tasks_completed = db.execute(
                """SELECT COUNT(*) FROM activity_events ae
                   JOIN sessions s ON s.id=ae.session_id
                   WHERE ae.kind='task_status' AND ae.to_status='completed'
                   AND ae.occurred_at >= ?
                   AND ae.session_id NOT IN (SELECT session_id FROM forgotten_sessions)""",
                (start_time,),
            ).fetchone()[0]

            jobs_completed = db.execute(
                """SELECT COUNT(*) FROM scheduled_runs sr
                   WHERE sr.status='completed' AND sr.started_at >= ?""",
                (start_time,),
            ).fetchone()[0]

            cost_result = db.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) FROM turns t
                   JOIN sessions s ON s.id=t.session_id
                   WHERE t.cost_usd IS NOT NULL AND t.ended_at >= ?
                   AND t.session_id NOT IN (SELECT session_id FROM forgotten_sessions)""",
                (start_time,),
            ).fetchone()
            cost_usd = float(cost_result[0]) if cost_result[0] else 0.0

            return {
                "answers": answers,
                "actions": actions,
                "tasksCompleted": tasks_completed,
                "jobsCompleted": jobs_completed,
                "costUsd": round(cost_usd, 6),
            }

    today = _count_period(day_start)
    week = _count_period(week_start)

    return {"today": today, "week": week}


def inbox(store, *, now=None):
    """Generate the inbox view of everything that happened and everything that needs the owner."""
    now = now or time.time()
    zone, zone_name = _owner_zone(store)
    today = datetime.fromtimestamp(now, zone).date()
    day_start = _local_midnight(today, zone)
    # The week is the last seven local days, today included (DST-safe: local dates, not seconds).
    week_start = _local_midnight(today - timedelta(days=6), zone)

    needs_you = []
    needs_you.extend(_needs_approval_items(store, day_start))
    needs_you.extend(_needs_suggestion_items(store))
    needs_you.extend(_needs_memory_items(store))
    needs_you.extend(_needs_attention_items(store))
    needs_you.extend(plan_alerts.needs_you(store))
    needs_you.extend(workflow_proposals.needs_you(store))
    needs_you.extend(workflow_offers.needs_you(store))
    needs_you.extend(agent_checks.needs_you(store))

    needs_you.sort(key=lambda x: (not x.get("urgency", {}).get("urgent", False), x["since"]))
    needs_you = needs_you[:50]  # Limit to 50

    handled = sorted(
        _handled_items(store, day_start) + plan_alerts.resets(store, day_start),
        key=lambda item: item["at"],
        reverse=True,
    )[:50]
    upcoming = _upcoming_items(store, now)[:20]  # Already sorted soonest first

    impact = _impact_counts(store, day_start, week_start)

    return {
        "generatedAt": now,
        "day": {
            "date": today.isoformat(),
            "timeZone": zone_name,
            "startsAt": day_start,
        },
        "needsYou": needs_you,
        "handled": handled,
        "upcoming": upcoming,
        "impact": impact,
        "counts": {"needsYou": len(needs_you), "handled": len(handled), "upcoming": len(upcoming)},
    }
