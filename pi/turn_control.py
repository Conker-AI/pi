"""Durable owner stop requests; in-flight effects must still be reconciled."""

import time

from . import tasks
from .providers import ProviderUnavailable

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_cancellations (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    requested_at REAL NOT NULL
);
"""


def cancel(store, turn_id):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM turns WHERE id=? AND session_id NOT IN "
            "(SELECT session_id FROM forgotten_sessions)",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise tasks.TaskError("not_found", "Turn not found.", 404)
        prior = db.execute(
            "SELECT requested_at FROM turn_cancellations WHERE turn_id=?", (turn_id,)
        ).fetchone()
        if not prior:
            if row["status"] != "running":
                raise tasks.TaskError(
                    "turn_not_running", "Only a running turn can be stopped.", 409
                )
            db.execute("INSERT INTO turn_cancellations VALUES (?,?)", (turn_id, time.time()))
        requested = db.execute(
            "SELECT requested_at FROM turn_cancellations WHERE turn_id=?", (turn_id,)
        ).fetchone()[0]
        db.commit()
        return {
            "turn_id": turn_id,
            "cancel_requested": True,
            "requested_at": requested,
            "status": row["status"],
            "acted": bool(row["acted"]),
        }


def guard_db(db, turn_id):
    if (
        turn_id
        and db.execute("SELECT 1 FROM turn_cancellations WHERE turn_id=?", (turn_id,)).fetchone()
    ):
        raise ProviderUnavailable("Owner stopped this turn; completed effects are retained.")


def guard(store, execution):
    from . import calls

    calls.guard(store, execution)
    turn_id = (execution or {}).get("turnExecutionId")
    if turn_id:
        with store._connect() as db:
            guard_db(db, turn_id)
