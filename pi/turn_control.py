"""Durable owner stop requests; in-flight effects must still be reconciled."""

import re
import time

from . import tasks
from .providers import ProviderUnavailable

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_cancellations (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    requested_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_reply_recoveries (
    request_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(id),
    cancelled_at REAL,
    created_at REAL NOT NULL
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
    if (
        prior
        and row["status"] == "running"
        and db.execute(
            "SELECT 1 FROM turn_reply_recoveries WHERE turn_id=? AND cancelled_at=?",
            (turn_id, prior[0]),
        ).fetchone()
    ):
        # A new stop invalidates the permission issued for the previous stop.
        db.execute(
            "UPDATE turn_cancellations SET requested_at=? WHERE turn_id=?",
            (max(time.time(), prior[0] + 0.000001), turn_id),
        )
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


def guard_db(db, turn_id, reply_request_id=None):
    if not turn_id:
        return
    stopped = db.execute(
        "SELECT requested_at FROM turn_cancellations WHERE turn_id=?", (turn_id,)
    ).fetchone()
    if stopped:
        consent = db.execute(
            "SELECT 1 FROM turn_reply_recoveries WHERE request_id=? "
            "AND turn_id=? AND cancelled_at=?",
            (reply_request_id, turn_id, stopped[0]),
        ).fetchone()
        if not consent:
            raise ProviderUnavailable("Owner stopped this turn; completed effects are retained.")


def claim_reply(store, turn_id, request_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
        raise tasks.TaskError("invalid_request_id", "Use an 8-128 character request ID.", 422)
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
            "SELECT turn_id FROM turn_reply_recoveries WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior:
            if prior[0] != turn_id:
                raise tasks.TaskError("request_conflict", "Request ID belongs to another turn.")
            return dict(row), False
        if row["status"] != "acted_no_reply" or not row["acted"]:
            raise tasks.TaskError(
                "reply_unavailable", "Only a completed action awaiting its reply can recover."
            )
        if (
            not db.execute(
                "SELECT 1 FROM tool_actions WHERE turn_id=? AND state='completed'", (turn_id,)
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM tool_actions WHERE turn_id=? "
                "AND state NOT IN ('completed','refused')",
                (turn_id,),
            ).fetchone()
        ):
            raise tasks.TaskError(
                "unresolved_action", "Reconcile all action outcomes before asking for a reply."
            )
        if (
            db.execute(
                "SELECT 1 FROM turns WHERE session_id=? AND id!=? AND status='running'",
                (row["session_id"], turn_id),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                (row["session_id"],),
            ).fetchone()
        ):
            raise tasks.TaskError("session_busy", "Another request is using this conversation.")
        cancelled = db.execute(
            "SELECT requested_at FROM turn_cancellations WHERE turn_id=?", (turn_id,)
        ).fetchone()
        db.execute(
            "INSERT INTO turn_reply_recoveries VALUES(?,?,?,?)",
            (request_id, turn_id, cancelled[0] if cancelled else None, time.time()),
        )
        db.execute("UPDATE turns SET status='running',ended_at=NULL WHERE id=?", (turn_id,))
        db.commit()
        return dict(row), True


def guard(store, execution):
    from . import calls

    calls.guard(store, execution)
    turn_id = (execution or {}).get("turnExecutionId")
    request_id = (execution or {}).get("submissionExecutionId")
    if turn_id or request_id:
        with store._connect() as db:
            guard_db(db, turn_id, (execution or {}).get("replyRecoveryId"))
            if request_id:
                row = db.execute(
                    "SELECT failure_code FROM turn_submissions WHERE request_id=?", (request_id,)
                ).fetchone()
                if row and row[0] == "owner_cancelled":
                    raise ProviderUnavailable("Owner stopped this submission during preparation.")
