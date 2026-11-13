"""Inbox notices: a one-line heads-up, maybe a link to a chat, and Dismiss. No answer, no undo.

One table for every kind. Each kind keeps its own reader, item shape and dismiss route:
- `plan` (pi.owner.plan_alerts): a plan window passed the owner's level. `reset_at` is set when
  the window's period ends, and a later period can raise a new one.
- `agent-job` (pi.schedule.agent_jobs): an AI job's reply had something for the owner. It links to
  the job's chat (`session_id`) and is forgotten with it (`redact`). It never resets.

`subject` is the kind's key: at most one notice per (kind, subject) while `reset_at` is NULL. A
plan alert's subject is `provider:window`, so one per window period. A note's subject is its run,
so one per run. `facts` holds the kind's own fields as JSON. The kind is checked here, not by a
CHECK, so a new kind needs no table rebuild.

Migration 0006 (`merge_old_tables`) moved the rows of the two tables these kinds had before,
`plan_alerts` and `agent_job_notes`, keeping their ids, and dropped those tables.
"""

from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL,
 subject TEXT NOT NULL,
 title TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '',
 session_id TEXT,
 facts TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL,
 reset_at REAL,
 dismissed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS notices_one_open ON notices(kind, subject) WHERE reset_at IS NULL;
CREATE INDEX IF NOT EXISTS notices_open ON notices(kind, dismissed_at, created_at);
"""
PLAN = "plan"
AGENT_JOB = "agent-job"
KINDS = frozenset({PLAN, AGENT_JOB})


# A note whose chat was forgotten. Normally `redact` removed it with the chat; this catches one
# written while an older Pi ran on this database (a code rollback without the snapshot), which
# could neither see this table nor clear it.
_FORGOTTEN = "DELETE FROM notices WHERE session_id IN (SELECT session_id FROM forgotten_sessions)"


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    if _exists(db, "forgotten_sessions"):
        db.executescript(_FORGOTTEN + ";")


def _dump(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=False, sort_keys=True)


def facts(row) -> dict:
    """The kind's own fields of a notices row."""
    value = json.loads(row["facts"] or "{}")
    return value if isinstance(value, dict) else {}


def insert(
    db: sqlite3.Connection,
    *,
    identity: str,
    kind: str,
    subject: str,
    title: str,
    created_at: float,
    facts: dict,
    detail: str = "",
    session_id: str | None = None,
) -> bool:
    """Add a notice; False when one for this (kind, subject) is already open."""
    if kind not in KINDS:
        raise ValueError(f"Unknown notice kind: {kind}")
    cursor = db.execute(
        "INSERT OR IGNORE INTO notices(id,kind,subject,title,detail,session_id,facts,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (identity, kind, subject, title, detail, session_id, _dump(facts), created_at),
    )
    return cursor.rowcount == 1


def update(db: sqlite3.Connection, identity: str, *, title: str, facts: dict) -> None:
    db.execute("UPDATE notices SET title=?, facts=? WHERE id=?", (title, _dump(facts), identity))


def _exists(db: sqlite3.Connection, table: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return found.fetchone() is not None


def redact(db: sqlite3.Connection, session_ids: list[str]) -> None:
    """Forget with the chat, inside the caller's forgetting transaction: a note quotes its reply.

    A notice with no chat (a plan alert) is never touched.
    """
    if not session_ids or not _exists(db, "notices"):
        return
    db.executemany("DELETE FROM notices WHERE session_id=?", [(value,) for value in session_ids])
    if _exists(db, "forgotten_sessions"):
        db.execute(_FORGOTTEN)


def merge_old_tables(db: sqlite3.Connection) -> None:
    """Migration 0006: move plan_alerts and agent_job_notes rows here, ids kept, then drop them.

    Runs inside the ledger's transaction, so it uses execute only (executescript would commit).
    A database where neither old table exists is left as it is.
    """
    if _exists(db, "plan_alerts"):
        _merge_plan_alerts(db)
        db.execute("DROP TABLE plan_alerts")
    if _exists(db, "agent_job_notes"):
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
                PLAN,
                plan_alerts.subject(row["provider"], row["window_id"]),
                plan_alerts.title(fields, reset=False),
                "",
                None,
                _dump(fields),
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
                AGENT_JOB,
                row["run_id"],
                row["title"],
                row["detail"],
                row["session_id"],
                _dump({"jobId": row["job_id"], "messageId": row["message_id"]}),
                row["created_at"],
                None,
                dismissed,
            ),
        )
