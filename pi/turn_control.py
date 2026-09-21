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


def _cancel_db(db, turn_id):
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
            raise tasks.TaskError("turn_not_running", "Only a running turn can be stopped.", 409)
        db.execute("INSERT INTO turn_cancellations VALUES (?,?)", (turn_id, time.time()))
    requested = db.execute(
        "SELECT requested_at FROM turn_cancellations WHERE turn_id=?", (turn_id,)
    ).fetchone()[0]
    return {
        "turn_id": turn_id,
        "cancel_requested": True,
        "requested_at": requested,
        "status": row["status"],
        "acted": bool(row["acted"]),
    }


def cancel(store, turn_id):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        result = _cancel_db(db, turn_id)
        db.commit()
        return result


def cancel_submission(store, request_id):
    from . import attachment_turns, submissions

    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = submissions._row(db, request_id)
        if row["state"] == "forgotten":
            raise tasks.TaskError("not_found", "Submission not found.", 404)
        if row["turn_id"]:
            _cancel_db(db, row["turn_id"])
        elif row["state"] == "preparing":
            db.execute(
                "UPDATE turn_submissions SET state='preparation_failed',"
                "failure_code='owner_cancelled',updated_at=? WHERE request_id=?",
                (time.time(), request_id),
            )
            attachment_turns.release(db, request_id)
            db.execute(
                "UPDATE submission_context SET state='interrupted',"
                "evidence=? WHERE request_id=? AND state IN ('reserved','running')",
                ('{"error":"owner_cancelled"}', request_id),
            )
        elif row["failure_code"] != "owner_cancelled":
            raise tasks.TaskError("not_preparing", "This submission has already finished.", 409)
        result = submissions._view(db, submissions._row(db, request_id))
        db.commit()
        return result


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
    request_id = (execution or {}).get("submissionExecutionId")
    if turn_id or request_id:
        with store._connect() as db:
            guard_db(db, turn_id)
            if request_id:
                row = db.execute(
                    "SELECT failure_code FROM turn_submissions WHERE request_id=?", (request_id,)
                ).fetchone()
                if row and row[0] == "owner_cancelled":
                    raise ProviderUnavailable("Owner stopped this submission during preparation.")
