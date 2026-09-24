"""Caller-side identities, committed before ToolGate can dispatch anything."""

import json
import time

from . import submissions

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_actions (
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
    tool_id TEXT NOT NULL, args TEXT, job_id TEXT,
    state TEXT NOT NULL DEFAULT 'dispatching', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_actions_turn ON tool_actions(turn_id);
"""


def prepare(store, turn_id, tool_id, args, action_id, job_id=None, *, proposal=None):
    from . import turn_steering

    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        turn_steering.guard_db(db, turn_id)
        if proposal is not None:
            turn = db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)).fetchone()
            submissions.append(
                db, turn[0], "assistant", proposal, turn_id=turn_id, purpose="intermediate"
            )
        db.execute(
            "INSERT INTO tool_actions VALUES(?,?,?,?,?,'dispatching',?)",
            (action_id, turn_id, tool_id, json.dumps(args), job_id, time.time()),
        )
        db.commit()
    return latest(store, turn_id)


def latest(store, turn_id):
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM tool_actions WHERE turn_id=? ORDER BY rowid DESC LIMIT 1", (turn_id,)
        ).fetchone()
    return {**dict(row), "args": json.loads(row["args"] or "null")} if row else None


def state(store, action_id, value):
    with store._connect() as db:
        db.execute("UPDATE tool_actions SET state=? WHERE id=?", (value, action_id))


def bind_job(store, action_id, job_id):
    with store._connect() as db:
        changed = db.execute(
            "UPDATE tool_actions SET job_id=? WHERE id=? "
            "AND state IN ('awaiting_approval','awaiting_budget') "
            "AND (job_id IS NULL OR job_id=?)",
            (job_id, action_id, job_id),
        ).rowcount
        if not changed:
            raise ValueError(
                "A job can only be attached before approval dispatch; keep its original ID."
            )


def record(store, action, outcome):
    # The observation and acted flag must survive together, including process death.
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        turn = db.execute(
            "SELECT session_id,status FROM turns WHERE id=?", (action["turn_id"],)
        ).fetchone()
        if turn["status"] != "running":
            raise RuntimeError("Turn is no longer claimed by this caller")
        previous = db.execute(
            "SELECT state FROM tool_actions WHERE id=?", (action["id"],)
        ).fetchone()
        if previous[0] == "completed":
            return
        observation = json.dumps(
            {"tool": outcome.tool_id, "ok": outcome.ok, "result": outcome.result},
            ensure_ascii=False,
        )
        submissions.append(
            db,
            turn["session_id"],
            "tool",
            observation,
            turn_id=action["turn_id"],
            purpose="tool_result",
            action_id=action["id"],
        )
        db.execute(
            "UPDATE turns SET acted=MAX(acted,?) WHERE id=?", (int(outcome.ok), action["turn_id"])
        )
        db.execute("UPDATE tool_actions SET state='completed' WHERE id=?", (action["id"],))
        db.commit()
