"""Exact owner-reviewed memory corrections with durable uncertain outcomes."""

import hashlib
import json
import time
from typing import Literal

from pydantic import Field, field_validator

from . import agents, session_settings
from .memory_corrections import CorrectionError


class Create(agents.StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    session_id: str = Field(min_length=1, max_length=200)
    memory_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    expected_memory_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=16000)
    reason: str = Field(min_length=1, max_length=2000)
    basis: Literal["stated", "inferred"]
    source_message_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("source_message_ids")
    @classmethod
    def source_ids(cls, values):
        return agents.references(values)


class Decision(agents.StrictModel):
    decision: Literal["apply", "reject"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_proposals (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 payload_hash TEXT NOT NULL, memory_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 expected_revision INTEGER NOT NULL, baseline TEXT, desired TEXT, reason TEXT,
 basis TEXT NOT NULL, source_ids TEXT NOT NULL, state TEXT NOT NULL,
 receipt TEXT, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS memory_proposals_no_delete BEFORE DELETE ON memory_proposals
BEGIN SELECT RAISE(ABORT,'proposal identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS memory_proposals_no_replace BEFORE INSERT ON memory_proposals
WHEN EXISTS(SELECT 1 FROM memory_proposals WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'proposal identities are permanent'); END;
"""
FIXED = """
CREATE TRIGGER IF NOT EXISTS memory_proposals_fixed BEFORE UPDATE ON memory_proposals
WHEN NEW.id IS NOT OLD.id OR NEW.session_id IS NOT OLD.session_id
 OR NEW.payload_hash IS NOT OLD.payload_hash OR NEW.memory_id IS NOT OLD.memory_id
 OR NEW.agent_id IS NOT OLD.agent_id OR NEW.expected_revision IS NOT OLD.expected_revision
 OR NEW.baseline IS NOT OLD.baseline OR NEW.desired IS NOT OLD.desired
 OR NEW.reason IS NOT OLD.reason OR NEW.basis IS NOT OLD.basis
 OR NEW.source_ids IS NOT OLD.source_ids OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.state IN ('applied','rejected','conflict','failed','forgotten'))
BEGIN SELECT RAISE(ABORT,'reviewed proposal is immutable'); END;
"""
SCHEMA += FIXED


def _error(code, message, status=409):
    raise agents.AgentError(code, message, status)


def _eligible(db, session, sources):
    privacy = session_settings.source_privacy(db, session)
    if privacy is None or privacy["memoryDisabled"]:
        _error("private_source", "This conversation cannot propose long-term memory changes.")
    if len(set(sources)) != len(sources):
        _error("duplicate_source", "Select distinct source messages.", 422)
    for identity in sources:
        row = db.execute(
            "SELECT role FROM messages WHERE id=? AND session_id=?", (identity, session)
        ).fetchone()
        if row is None or row["role"] != "user":
            _error("invalid_source", "Evidence must cite owner messages in this conversation.", 422)


def _row(db, identity):
    row = db.execute("SELECT * FROM memory_proposals WHERE id=?", (identity,)).fetchone()
    if row is None or row["state"] == "forgotten":
        _error("not_found", "Memory proposal unavailable.", 404)
    return row


def _view(row):
    return (
        {
            key: json.loads(row[key]) if row[key] is not None else None
            for key in ("baseline", "source_ids", "receipt")
        }
        | {
            key: row[key]
            for key in (
                "id",
                "session_id",
                "memory_id",
                "agent_id",
                "expected_revision",
                "desired",
                "reason",
                "basis",
                "state",
                "created_at",
            )
        }
        | {"retention": "independent-reviewed-memory-after-apply"}
    )


def get(store, identity):
    with store._connect() as db:
        return _view(_row(db, identity))


def list_all(store, session_id):
    with store._connect() as db:
        session_settings._load(db, session_id)
        return [
            _view(row)
            for row in db.execute(
                "SELECT * FROM memory_proposals WHERE session_id=? AND state!=? "
                "ORDER BY created_at DESC LIMIT 100",
                (session_id, "forgotten"),
            )
        ]


def create(store, body, client):
    body = Create.model_validate(body.model_dump())
    if not body.text.strip() or not body.reason.strip():
        _error("empty_text", "Correction and reason must contain text.", 422)
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with store._connect() as db:
        old = db.execute("SELECT * FROM memory_proposals WHERE id=?", (body.request_id,)).fetchone()
        if old:
            if old["state"] == "forgotten" or old["payload_hash"] != digest:
                _error("request_conflict", "Proposal identity is unavailable or already used.")
            return _view(old)
        _eligible(db, body.session_id, body.source_message_ids)
    if client is None:
        _error("not_configured", "Memory correction transport is not configured.", 503)
    try:
        baseline = client.memory(body.memory_id)
    except Exception:
        _error("source_unavailable", "Cannot load the authoritative memory for review.", 503)
    if baseline["revision"] != body.expected_memory_revision:
        _error("revision_conflict", "Memory changed; reload before proposing a correction.")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _eligible(db, body.session_id, body.source_message_ids)
        old = db.execute("SELECT * FROM memory_proposals WHERE id=?", (body.request_id,)).fetchone()
        if old:
            if old["payload_hash"] != digest:
                _error("request_conflict", "Proposal identity was already used.")
            return _view(old)
        db.execute(
            "INSERT INTO memory_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,NULL,?)",
            (
                body.request_id,
                body.session_id,
                digest,
                body.memory_id,
                client.agent_id,
                baseline["revision"],
                json.dumps(baseline),
                body.text,
                body.reason,
                body.basis,
                json.dumps(body.source_message_ids),
                "pending",
                time.time(),
            ),
        )
        db.commit()
    return get(store, body.request_id)


def _receipt(row, receipt):
    if (
        not isinstance(receipt, dict)
        or receipt.get("request_id") != row["id"]
        or receipt.get("memory_id") != row["memory_id"]
        or receipt.get("agent_id") != row["agent_id"]
        or receipt.get("previous_revision") != row["expected_revision"]
        or type(receipt.get("previous_revision")) is not int
        or type(receipt.get("revision")) is not int
        or receipt.get("revision") != row["expected_revision"] + 1
        or receipt.get("status") != "applied"
    ):
        raise ValueError("Correction receipt does not match the approved proposal.")
    result = {
        key: receipt[key]
        for key in (
            "request_id",
            "memory_id",
            "agent_id",
            "previous_revision",
            "revision",
            "status",
        )
    }
    result["indexing"] = receipt.get("indexing", "unknown")
    if result["indexing"] not in ("pending", "indexed", "degraded", "unknown"):
        raise ValueError("Invalid correction indexing status.")
    return result


def decide(store, identity, body, client):
    body = Decision.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        if row["state"] != "pending":
            return _view(row)
        if body.decision == "reject":
            db.execute("UPDATE memory_proposals SET state='rejected' WHERE id=?", (identity,))
            db.commit()
            return get(store, identity)
        _eligible(db, row["session_id"], json.loads(row["source_ids"]))
        if client is None or client.agent_id != row["agent_id"]:
            _error("not_configured", "Original correction namespace is unavailable.", 503)
        db.execute("UPDATE memory_proposals SET state='applying' WHERE id=?", (identity,))
        db.commit()
    state, receipt = "unknown", None
    try:
        receipt = _receipt(
            row, client.apply(identity, row["memory_id"], row["expected_revision"], row["desired"])
        )
        state = "applied"
    except CorrectionError as exc:
        if exc.status == 409:
            state = "conflict"
        elif exc.status in (400, 401, 403, 404, 422):
            state = "failed"
    except Exception:
        pass  # Response/transport errors may occur after the remote commit.
    with store._connect() as db:
        db.execute(
            "UPDATE memory_proposals SET state=?,receipt=? WHERE id=? AND state='applying'",
            (state, json.dumps(receipt) if receipt else None, identity),
        )
    return get(store, identity)


def reconcile(store, identity, client):
    with store._connect() as db:
        row = _row(db, identity)
        if row["state"] not in ("applying", "unknown"):
            return _view(row)
    if client is None or client.agent_id != row["agent_id"]:
        _error("not_configured", "Original correction namespace is unavailable.", 503)
    try:
        receipt = _receipt(row, client.receipt(identity))
    except Exception:
        return get(store, identity)  # Even a 404 cannot rule out an in-flight commit.
    with store._connect() as db:
        db.execute(
            "UPDATE memory_proposals SET state='applied',receipt=? WHERE id=? "
            "AND state IN ('applying','unknown')",
            (json.dumps(receipt), identity),
        )
    return get(store, identity)


def recover_interrupted(store):
    with store._connect() as db:
        return db.execute(
            "UPDATE memory_proposals SET state='unknown' WHERE state='applying'"
        ).rowcount


def redact(db, sessions):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_proposals'").fetchone():
        return
    db.execute("DROP TRIGGER memory_proposals_fixed")
    for session in sessions:
        db.execute(
            "UPDATE memory_proposals SET baseline=NULL,desired=NULL,reason=NULL,"
            "payload_hash='',source_ids='[]',receipt=NULL,state='forgotten' WHERE session_id=?",
            (session,),
        )
    db.execute(FIXED)
