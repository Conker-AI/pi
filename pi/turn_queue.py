"""Owner-managed future turns; enqueueing grants no execution authority."""

import hashlib
import json
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from . import agents, attachment_turns, attachments, session_settings, submissions, tasks

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_queues (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), paused INTEGER NOT NULL DEFAULT 0,
 reason TEXT, revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS queued_turns (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 revision INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT, snapshot TEXT,
 original_hash TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 submission_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS queued_turn_order ON queued_turns(session_id,created_at,id);
"""


class Enqueue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=4000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class QueueRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _snapshot(db, sid):
    value = session_settings._snapshot(db, sid)
    if value.get("callExecution") or value["kind"] == "team-role":
        raise tasks.TaskError("queue_unavailable", "Queue ordinary conversations only.")
    context = db.execute(
        "SELECT revision,policy FROM context_policies WHERE session_id=?", (sid,)
    ).fetchone()
    return _encode({"execution": value, "context": dict(context) if context else None})


def _source(db, sid):
    tasks._source(db, sid, open_required=True)
    db.execute("INSERT OR IGNORE INTO conversation_queues(session_id) VALUES(?)", (sid,))


def _files(db, sid, payload, snapshot):
    ids = payload["attachment_ids"]
    if len(ids) != len(set(ids)):
        raise tasks.TaskError("invalid_attachments", "Select distinct attachments.", 422)
    privacy = json.loads(snapshot)["execution"]["privacy"]
    texts = [attachment_turns._text(db, sid, identity, privacy) for identity in ids]
    if sum(len(item["text"]) for item in texts) > 32000:
        raise tasks.TaskError("input_limit", "Attached text exceeds 32000 characters.", 413)


def _entry(db, sid, identity, revision=None):
    row = db.execute(
        "SELECT * FROM queued_turns WHERE id=? AND session_id=?", (identity, sid)
    ).fetchone()
    if row is None:
        raise tasks.TaskError("not_found", "Queued message not found.", 404)
    if revision is not None and row["revision"] != revision:
        raise tasks.TaskError("revision_conflict", "Queued message changed; reload it.")
    return row


def _view(row):
    return {
        **{k: row[k] for k in ("id", "revision", "state", "created_at", "submission_id")},
        "payload": json.loads(row["payload"]) if row["payload"] else None,
        "selection": json.loads(row["snapshot"]) if row["snapshot"] else None,
    }


def _read(db, sid):
    queue = db.execute("SELECT * FROM conversation_queues WHERE session_id=?", (sid,)).fetchone()
    return {
        "session_id": sid,
        "paused": bool(queue["paused"]),
        "reason": queue["reason"],
        "revision": queue["revision"],
        "entries": [
            _view(row)
            for row in db.execute(
                "SELECT * FROM queued_turns WHERE session_id=? AND state IN ('waiting','claimed') "
                "ORDER BY created_at,id",
                (sid,),
            )
        ],
    }


def _pause(db, sid, reason):
    db.execute(
        "UPDATE conversation_queues SET paused=1,reason=?,revision=revision+1 "
        "WHERE session_id=? AND (paused=0 OR reason IS NOT ?)",
        (reason, sid, reason),
    )


def validate(db, row):
    if row["snapshot"] != _snapshot(db, row["session_id"]):
        raise tasks.TaskError(
            "queue_review_required", "Settings changed; review this queued message."
        )
    _files(db, row["session_id"], json.loads(row["payload"]), row["snapshot"])


def read(store, sid):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _source(db, sid)
        for row in db.execute(
            "SELECT * FROM queued_turns WHERE session_id=? AND state='waiting'", (sid,)
        ):
            try:
                validate(db, row)
            except (
                tasks.TaskError,
                agents.AgentError,
                attachments.AttachmentError,
                ValueError,
            ) as exc:
                _pause(db, sid, getattr(exc, "detail", {}).get("code", "selection_unavailable"))
                break
        result = _read(db, sid)
        db.commit()
        return result


def enqueue(store, sid, body):
    body = Enqueue.model_validate(body.model_dump())
    if not body.text.strip():
        raise tasks.TaskError("empty_message", "Write a message for the queued turn.", 422)
    payload = {"text": body.text, "attachment_ids": body.attachment_ids}
    digest = hashlib.sha256(_encode(payload).encode()).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _source(db, sid)
        prior = db.execute("SELECT * FROM queued_turns WHERE id=?", (body.request_id,)).fetchone()
        if prior:
            if prior["session_id"] != sid or prior["original_hash"] != digest:
                raise tasks.TaskError(
                    "request_conflict", "Queue request identity was already used."
                )
            return _view(prior)
        count = db.execute(
            "SELECT COUNT(*) FROM queued_turns WHERE session_id=? "
            "AND state IN ('waiting','claimed')",
            (sid,),
        ).fetchone()[0]
        if count >= 5:
            raise tasks.TaskError("queue_full", "Queue at most five messages.")
        snapshot = _snapshot(db, sid)
        _files(db, sid, payload, snapshot)
        now = time.time()
        db.execute(
            "INSERT INTO queued_turns VALUES(?,?,1,'waiting',?,?,?,?,?,NULL)",
            (body.request_id, sid, _encode(payload), snapshot, digest, now, now),
        )
        db.execute("UPDATE conversation_queues SET revision=revision+1 WHERE session_id=?", (sid,))
        result = _view(_entry(db, sid, body.request_id))
        db.commit()
        return result


def change(store, sid, identity, body, operation):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _source(db, sid)
        row = _entry(db, sid, identity, body.expected_revision)
        if row["state"] != "waiting":
            raise tasks.TaskError("queue_claimed", "This request is no longer editable.")
        payload, snapshot, state = json.loads(row["payload"]), row["snapshot"], row["state"]
        if operation == "edit":
            if not body.text.strip():
                raise tasks.TaskError("empty_message", "Write a message.", 422)
            payload["text"] = body.text
        elif operation == "review":
            snapshot = _snapshot(db, sid)
            _files(db, sid, payload, snapshot)
        elif operation == "remove":
            state, payload, snapshot = "removed", None, None
        else:
            raise ValueError("Unknown queue operation")
        db.execute(
            "UPDATE queued_turns SET revision=revision+1,state=?,payload=?,snapshot=?,"
            "updated_at=? WHERE id=?",
            (state, _encode(payload) if payload else None, snapshot, time.time(), identity),
        )
        db.execute("UPDATE conversation_queues SET revision=revision+1 WHERE session_id=?", (sid,))
        result = _view(_entry(db, sid, identity))
        db.commit()
        return result


def pause(store, sid, body, paused):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _source(db, sid)
        current = _read(db, sid)
        if current["revision"] != body.expected_revision:
            raise tasks.TaskError("revision_conflict", "Queue changed; reload before continuing.")
        reason = "owner_paused" if paused else None
        if not paused:
            for row in db.execute(
                "SELECT * FROM queued_turns WHERE session_id=? AND state='waiting'", (sid,)
            ):
                validate(db, row)
            if submissions._busy(db, sid):
                raise tasks.TaskError("session_busy", "Resolve the current turn before resuming.")
        db.execute(
            "UPDATE conversation_queues SET paused=?,reason=?,revision=revision+1 "
            "WHERE session_id=?",
            (int(paused), reason, sid),
        )
        result = _read(db, sid)
        db.commit()
        return result


def redact(db, sessions):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='queued_turns'").fetchone():
        return
    for sid in sessions:
        db.execute(
            "UPDATE queued_turns SET state='forgotten',payload=NULL,snapshot=NULL,"
            "original_hash=NULL WHERE session_id=?",
            (sid,),
        )
        db.execute(
            "UPDATE conversation_queues SET paused=1,reason='forgotten' WHERE session_id=?", (sid,)
        )


def router(get_store, owner):
    api = APIRouter(dependencies=[Depends(owner)])

    @api.get("/sessions/{sid}/queue")
    def listing(sid: str):
        return read(get_store(), sid)

    @api.post("/sessions/{sid}/queue")
    def add(sid: str, body: Enqueue):
        return enqueue(get_store(), sid, body)

    @api.patch("/sessions/{sid}/queue/{identity}")
    def edit(sid: str, identity: str, body: Edit):
        return change(get_store(), sid, identity, body, "edit")

    @api.post("/sessions/{sid}/queue/{identity}/review")
    def review(sid: str, identity: str, body: Revision):
        return change(get_store(), sid, identity, body, "review")

    @api.post("/sessions/{sid}/queue/{identity}/remove")
    def remove(sid: str, identity: str, body: Revision):
        return change(get_store(), sid, identity, body, "remove")

    @api.post("/sessions/{sid}/queue/pause")
    def stop(sid: str, body: QueueRevision):
        return pause(get_store(), sid, body, True)

    @api.post("/sessions/{sid}/queue/resume")
    def resume(sid: str, body: QueueRevision):
        return pause(get_store(), sid, body, False)

    return api
