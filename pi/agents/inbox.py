"""Inbox: everything that happened and everything that needs the owner."""

import json
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pi.agents import asks, change_requests, continuity
from pi.conversation import settings as session_settings
from pi.conversation import visibility
from pi.owner import plan_alerts, self_check, updates
from pi.owner import preferences as owner_preferences
from pi.schedule import agent_jobs, job_hidden, jobs, proposals

# A parked turn's detail once Pi ended it because its approval expired (turns/decision_sweep.py).
APPROVAL_EXPIRED = "The approval expired before you answered. Nothing was done."


def _owner_zone(store):
    """The owner's time zone and its name; UTC when no preferences are saved."""
    with store._connect() as db:
        row = db.execute("SELECT preferences FROM owner_preferences WHERE singleton=1").fetchone()
    if not row:
        return UTC, "UTC"
    name = owner_preferences.OwnerPreferences.model_validate_json(row["preferences"]).quietHours
    return ZoneInfo(name.timeZone), name.timeZone


# A job Conker kept from a forgotten or trashed chat is named in that chat's words; once the owner
# adopted it (createdBy owner) it is his. Same rule as the jobs list and the briefing.
_ADOPTED = job_hidden.ADOPTED


def _hidden_jobs(db) -> list[str]:
    kept = sorted(visibility.kept_from_hidden(db, "ai_job"))
    if not kept:
        return []
    marks = ",".join("?" * len(kept))
    query = f"SELECT id FROM scheduled_jobs WHERE id IN ({marks}) AND {_ADOPTED}"
    adopted = {row[0] for row in db.execute(query, kept)}
    return [job for job in kept if job not in adopted]


def _local_midnight(day, zone):
    return datetime.combine(day, datetime.min.time(), zone).timestamp()


def _needs_approval_items(store, day_start, turns):
    """Get items needing approval from turns."""
    items = []
    with store._connect() as db:
        for turn in turns:
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
        # A proposal from a hidden chat carries its words in the title (proposals.HIDDEN).
        rows = db.execute(
            f"SELECT * FROM proposals WHERE state='open' AND id NOT IN ({proposals.HIDDEN}) "
            "ORDER BY created_at"
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


def _needs_attention_items(store, turns):
    """Get continuity items needing attention."""
    items = []
    briefing = continuity.briefing(store, limit=100)

    approval_turn_ids = {turn["id"] for turn in turns}

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
        hidden_jobs = set(_hidden_jobs(db))

        for row in rows:
            if row["scheduled_run_id"]:
                # A run whose turn still waits on an approval is not handled yet.
                run = db.execute(
                    f"SELECT * FROM scheduled_runs WHERE id=? AND NOT ({jobs.WAITING_TURN})",
                    (row["scheduled_run_id"],),
                ).fetchone()
                if run and run["job_id"] not in hidden_jobs:
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


def _private(db, session_id) -> bool:
    privacy = session_settings.source_privacy(db, session_id)
    return privacy is not None and any(privacy.values())


def _expired_items(store, day_start):
    """Approvals that expired unanswered today: Pi ended the turn, nothing was done."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT t.id, t.session_id, t.approval_tool_id, t.ended_at, s.title FROM turns t "
            "JOIN sessions s ON s.id=t.session_id "
            "WHERE t.status='cancelled' AND t.detail=? AND t.ended_at >= ? "
            f"AND NOT {visibility.hidden_sql('t.session_id')} "
            "ORDER BY t.ended_at DESC LIMIT 50",
            (APPROVAL_EXPIRED, day_start),
        ).fetchall()
        private = {row["session_id"] for row in rows if _private(db, row["session_id"])}
    return [
        {
            "kind": "approval-expired",
            "title": f"Approval expired: {row['approval_tool_id'] or 'an action'}",
            "chat": "Private chat"
            if row["session_id"] in private
            else row["title"] or "Conversation",
            "at": row["ended_at"],
            "source": {"kind": "turn", "sessionId": row["session_id"], "turnId": row["id"]},
        }
        for row in rows
    ]


def _upcoming_items(store, now):
    """Get upcoming scheduled jobs and open tasks."""
    items = []
    week_later = now + (7 * 24 * 60 * 60)

    with store._connect() as db:
        # A turned-off job keeps its next_at but will not run.
        hidden_jobs = _hidden_jobs(db)
        marks = ",".join("?" * len(hidden_jobs))
        rows = db.execute(
            f"""SELECT * FROM scheduled_jobs
               WHERE next_at <= ? AND next_at > ?
               AND COALESCE(json_extract(definition,'$.enabled'), 1)
               AND id NOT IN ({marks})
               ORDER BY next_at ASC LIMIT 20""",
            (week_later, now, *hidden_jobs),
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

        # Open tasks (planned, in_progress, not archived, chat not hidden or private)
        task_rows = db.execute(
            f"""SELECT t.*, s.title as session_title
               FROM tasks t
               JOIN sessions s ON s.id=t.session_id
               WHERE t.status IN ('planned', 'in_progress')
               AND NOT {visibility.hidden_sql("t.session_id")}
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
                f"""SELECT COUNT(*) FROM scheduled_runs
                   WHERE status='completed' AND started_at >= ? AND NOT ({jobs.WAITING_TURN})""",
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
    approval_turns = store.awaiting_approval()
    needs_you.extend(_needs_approval_items(store, day_start, approval_turns))
    needs_you.extend(_needs_suggestion_items(store))
    needs_you.extend(_needs_attention_items(store, approval_turns))
    needs_you.extend(plan_alerts.needs_you(store))
    needs_you.extend(self_check.needs_you(store))
    needs_you.extend(updates.needs_you(store))
    needs_you.extend(change_requests.needs_you(store))
    needs_you.extend(asks.needs_you(store))
    needs_you.extend(agent_jobs.needs_you(store))

    needs_you.sort(key=lambda x: (not x.get("urgency", {}).get("urgent", False), x["since"]))
    needs_you = needs_you[:50]  # Limit to 50

    handled = sorted(
        _handled_items(store, day_start)
        + plan_alerts.resets(store, day_start)
        + self_check.recovered(store, day_start)
        + updates.applied(store, day_start)
        + _expired_items(store, day_start),
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
