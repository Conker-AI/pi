"""Migration 0006: the rows of the old plan_alerts and agent_job_notes tables move to notices.

Kept apart from pi.core.notices because it reads plan alerts' own subject and title.
"""

from __future__ import annotations

import sqlite3

from pi.core import notices


def merge_old_tables(db: sqlite3.Connection) -> None:
    """Migration 0006: move plan_alerts and agent_job_notes rows into notices, ids kept, then drop them.

    Runs inside the ledger's transaction, so it uses execute only (executescript would commit).
    A database where neither old table exists is left as it is.
    """
    if notices._exists(db, "plan_alerts"):
        _merge_plan_alerts(db)
        db.execute("DROP TABLE plan_alerts")
    if notices._exists(db, "agent_job_notes"):
        _merge_agent_job_notes(db)
        db.execute("DROP TABLE agent_job_notes")


_MOVE = (
    "INSERT OR IGNORE INTO notices(id,kind,subject,title,detail,session_id,facts,created_at,"
    "reset_at,dismissed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def _merge_plan_alerts(db: sqlite3.Connection) -> None:
    from pi.owner import plan_alerts

    for row in db.execute("SELECT * FROM plan_alerts").fetchall():
        fields = {
            "provider": row["provider"],
            "window": row["window_id"],
            "label": row["label"],
            "threshold": row["threshold"],
            "usedPercent": row["used_percent"],
            "resetsAt": row["resets_at"],
        }
        db.execute(
            _MOVE,
            (
                row["id"],
                notices.PLAN,
                plan_alerts.subject(row["provider"], row["window_id"]),
                plan_alerts.title(fields, reset=False),
                "",
                None,
                notices._dump(fields),
                row["raised_at"],
                row["reset_at"],
                row["dismissed_at"],
            ),
        )


def _merge_agent_job_notes(db: sqlite3.Connection) -> None:
    for row in db.execute("SELECT * FROM agent_job_notes").fetchall():
        dismissed = None
        if row["state"] == "dismissed":
            dismissed = row["dismissed_at"] or row["created_at"]
        db.execute(
            _MOVE,
            (
                row["id"],
                notices.AGENT_JOB,
                row["run_id"],
                row["title"],
                row["detail"],
                row["session_id"],
                notices._dump({"jobId": row["job_id"], "messageId": row["message_id"]}),
                row["created_at"],
                None,
                dismissed,
            ),
        )
