"""Durable owner steering for the ordinary running-turn planning boundary."""

import hashlib
import json
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from . import submissions, tasks

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_steering_controls (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turn_steers (
 request_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
 message_id TEXT NOT NULL UNIQUE REFERENCES messages(id), payload_hash TEXT,
 created_at REAL NOT NULL, applied_at REAL
);
CREATE TABLE IF NOT EXISTS steering_discarded_answers (
 id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
 provider TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER,
 output_tokens INTEGER, cached_tokens INTEGER, cost_usd REAL, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS steer_no_replace BEFORE INSERT ON turn_steers
WHEN EXISTS(SELECT 1 FROM turn_steers WHERE request_id=NEW.request_id)
BEGIN SELECT RAISE(ABORT,'steering identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS steer_no_delete BEFORE DELETE ON turn_steers
BEGIN SELECT RAISE(ABORT,'steering identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS steer_identity_fixed BEFORE UPDATE ON turn_steers
WHEN NEW.request_id IS NOT OLD.request_id OR NEW.turn_id IS NOT OLD.turn_id
 OR NEW.message_id IS NOT OLD.message_id OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.applied_at IS NOT NULL AND NEW.applied_at IS NOT OLD.applied_at)
 OR (NEW.payload_hash IS NOT OLD.payload_hash AND NEW.payload_hash IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'steering input is immutable'); END;
"""


class Pending(Exception):
    """Discard stale model output and rebuild at the same turn's next boundary."""


class Steer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=4000)


def active(store, turn_id, value):
    with store._connect() as db:
        db.execute(
            "INSERT INTO turn_steering_controls VALUES(?,?) ON CONFLICT(turn_id) "
            "DO UPDATE SET active=excluded.active",
            (turn_id, int(value)),
        )


def _view(db, row):
    turn = db.execute(
        "SELECT session_id,status FROM turns WHERE id=?", (row["turn_id"],)
    ).fetchone()
    tasks._source(db, turn["session_id"])
    state = (
        "applied"
        if row["applied_at"] is not None
        else ("pending" if turn["status"] == "running" else "not_applied")
    )
    result = dict(row)
    result.pop("payload_hash")
    return {**result, "state": state, "turn_status": turn["status"]}


def submit(store, turn_id, body):
    body = Steer.model_validate(body.model_dump())
    if not body.text.strip():
        raise tasks.TaskError("empty_steering", "Enter an instruction.", 422)
    digest = hashlib.sha256(body.text.encode()).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        turn = db.execute("SELECT session_id,status FROM turns WHERE id=?", (turn_id,)).fetchone()
        if turn is None:
            raise tasks.TaskError("not_found", "Turn not found.", 404)
        tasks._source(db, turn["session_id"])
        snapshot = db.execute(
            "SELECT snapshot FROM turn_settings WHERE turn_id=?", (turn_id,)
        ).fetchone()
        execution = json.loads(snapshot[0]) if snapshot else {}
        if execution.get("callExecution") or execution.get("kind") == "team-role":
            raise tasks.TaskError("steering_scope", "Use the call or team controls for this turn.")
        prior = db.execute(
            "SELECT * FROM turn_steers WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if prior:
            if prior["turn_id"] != turn_id or prior["payload_hash"] != digest:
                raise tasks.TaskError("request_conflict", "Steering identity was already used.")
            return {**_view(db, prior), "replayed": True}
        if (
            turn["status"] != "running"
            or not db.execute(
                "SELECT 1 FROM turn_steering_controls WHERE turn_id=? AND active=1", (turn_id,)
            ).fetchone()
        ):
            raise tasks.TaskError(
                "steering_unavailable",
                "Steer an actively running conversation turn; otherwise send a new message.",
            )
        if db.execute("SELECT 1 FROM turn_cancellations WHERE turn_id=?", (turn_id,)).fetchone():
            raise tasks.TaskError("turn_stopped", "This turn is already stopping.")
        if db.execute(
            "SELECT 1 FROM tool_actions WHERE turn_id=? AND state NOT IN ('completed','refused')",
            (turn_id,),
        ).fetchone():
            raise tasks.TaskError(
                "action_in_flight",
                "A tool action is already admitted. Wait for its result or stop future work; "
                "steering cannot retract it.",
            )
        if (
            db.execute("SELECT COUNT(*) FROM turn_steers WHERE turn_id=?", (turn_id,)).fetchone()[0]
            >= 10
        ):
            raise tasks.TaskError(
                "steering_limit",
                "This turn reached ten steering instructions; finish or stop it first.",
            )
        message = submissions.append(db, turn["session_id"], "user", body.text)
        db.execute(
            "INSERT INTO turn_steers VALUES(?,?,?,?,?,NULL)",
            (body.request_id, turn_id, message["id"], digest, time.time()),
        )
        row = db.execute(
            "SELECT * FROM turn_steers WHERE request_id=?", (body.request_id,)
        ).fetchone()
        result = _view(db, row)
        db.commit()
        return {**result, "replayed": False}


def consume(store, turn_id, message_ids, known_ids):
    from . import context_controls

    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM turns WHERE id=? AND status='running'", (turn_id,)
        ).fetchone():
            return
        for row in db.execute(
            "SELECT message_id FROM turn_steers WHERE turn_id=? AND applied_at IS NULL", (turn_id,)
        ):
            if row[0] in known_ids and row[0] not in message_ids:
                raise context_controls.ContextError(
                    "steering_excluded",
                    "Include the steering instruction in context before continuing.",
                )
        for identity in message_ids:
            db.execute(
                "UPDATE turn_steers SET applied_at=? WHERE turn_id=? "
                "AND message_id=? AND applied_at IS NULL",
                (time.time(), turn_id, identity),
            )
        db.commit()


def guard_db(db, turn_id):
    if db.execute(
        "SELECT 1 FROM turn_steers WHERE turn_id=? AND applied_at IS NULL", (turn_id,)
    ).fetchone():
        raise Pending()


def guard(store, execution):
    if (execution or {}).get("turnExecutionId"):
        with store._connect() as db:
            guard_db(db, execution["turnExecutionId"])


def discarded(store, turn_id, completion):
    with store._connect() as db:
        db.execute(
            "INSERT INTO steering_discarded_answers "
            "(turn_id,provider,model,input_tokens,output_tokens,cached_tokens,cost_usd,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                turn_id,
                completion.provider,
                completion.model,
                completion.input_tokens,
                completion.output_tokens,
                completion.cached_tokens,
                completion.cost_usd,
                time.time(),
            ),
        )


def account(db, turn_id, fields):
    rows = db.execute(
        "SELECT * FROM steering_discarded_answers WHERE turn_id=?", (turn_id,)
    ).fetchall()
    if rows:
        for key in ("input_tokens", "output_tokens", "cached_tokens", "cost_usd"):
            values = [fields.get(key)] + [row[key] for row in rows]
            fields[key] = sum(values) if all(value is not None for value in values) else None
    return fields


def redact(db, sessions):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_steers'").fetchone():
        for sid in sessions:
            db.execute(
                "UPDATE turn_steers SET payload_hash=NULL WHERE turn_id IN "
                "(SELECT id FROM turns WHERE session_id=?)",
                (sid,),
            )


def router(get_store, owner):
    api = APIRouter(dependencies=[Depends(owner)])

    @api.post("/turns/{turn_id}/steer")
    def send(turn_id: str, body: Steer):
        return submit(get_store(), turn_id, body)

    @api.get("/turns/{turn_id}/steering")
    def read(turn_id: str):
        with get_store()._connect() as db:
            turn = db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)).fetchone()
            if turn is None:
                raise tasks.TaskError("not_found", "Turn not found.", 404)
            tasks._source(db, turn[0])
            return {
                "discarded_answers": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM steering_discarded_answers WHERE turn_id=? ORDER BY id",
                        (turn_id,),
                    )
                ],
                "results": [
                    _view(db, row)
                    for row in db.execute(
                        "SELECT * FROM turn_steers WHERE turn_id=? ORDER BY created_at,request_id",
                        (turn_id,),
                    )
                ],
            }

    return api
