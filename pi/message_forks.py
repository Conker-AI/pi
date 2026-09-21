"""Exact message-boundary branches retain source references without copying evidence."""

import hashlib
import json
import time
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from . import session_settings, submissions, tasks

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_fork_requests (
 request_id TEXT PRIMARY KEY, source_session TEXT NOT NULL REFERENCES sessions(id),
 message_id TEXT NOT NULL REFERENCES messages(id),
 child_session TEXT NOT NULL REFERENCES sessions(id),
 payload_hash TEXT NOT NULL
);
"""


class Fork(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_settings_revision: int = Field(ge=0)
    expected_context_revision: int = Field(ge=0)
    reviewed_summary: str | None = Field(default=None, max_length=16000)


def create(store, sid, message_id, body):
    body = Fork.model_validate(body.model_dump())
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        tasks._source(db, sid)
        prior = db.execute(
            "SELECT * FROM message_fork_requests WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if prior:
            if (
                prior["source_session"] != sid
                or prior["message_id"] != message_id
                or prior["payload_hash"] != digest
            ):
                raise tasks.TaskError("request_conflict", "Fork request identity was already used.")
            tasks._source(db, prior["child_session"])
            return {
                "session_id": prior["child_session"],
                "parent_id": sid,
                "message_id": message_id,
                "replayed": True,
            }
        if (
            submissions._busy(db, sid)
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                (sid,),
            ).fetchone()
        ):
            raise tasks.TaskError("session_busy", "Resolve the current turn before branching.")
        snapshot = session_settings._snapshot(db, sid)
        if snapshot.get("callExecution") or snapshot["kind"] == "team-role":
            raise tasks.TaskError("fork_scope", "Branch an ordinary conversation.")
        settings = session_settings._load(db, sid)
        policy = db.execute(
            "SELECT revision,policy FROM context_policies WHERE session_id=?", (sid,)
        ).fetchone()
        if (
            settings["revision"] != body.expected_settings_revision
            or (policy[0] if policy else 0) != body.expected_context_revision
        ):
            raise tasks.TaskError(
                "revision_conflict", "Settings or context changed; review the branch again."
            )
        inherited = [
            row[0]
            for row in db.execute(
                "SELECT message_id FROM context_inherited_messages "
                "WHERE session_id=? ORDER BY position",
                (sid,),
            )
        ]
        from . import response_versions
        own = [row["id"] for row in response_versions.project(db, sid, [
            dict(row) for row in db.execute(
                "SELECT id FROM messages WHERE session_id=? ORDER BY seq", (sid,)
            )
        ])]
        ids = inherited + own
        if message_id not in ids:
            raise tasks.TaskError("foreign_message", "Choose a message in this conversation.", 404)
        prefix = ids[: ids.index(message_id) + 1]
        if len(prefix) > 5000:
            raise tasks.TaskError(
                "fork_limit", "Review a summary before branching over 5000 messages."
            )
        if policy and any(
            key not in prefix and mode == "keep-exact"
            for key, mode in json.loads(policy[1])["messagePolicies"].items()
        ):
            raise tasks.TaskError(
                "pin_after_boundary", "Review exact pins after this boundary before branching."
            )
        target = db.execute("SELECT role FROM messages WHERE id=?", (message_id,)).fetchone()
        association = db.execute(
            "SELECT purpose FROM turn_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if target[0] not in {"user", "assistant"} or (
            association and association[0] not in {"input", "final"}
        ):
            raise tasks.TaskError("fork_boundary", "Choose an input message or a final response.")
        parent = db.execute("SELECT title,summary FROM sessions WHERE id=?", (sid,)).fetchone()
        if parent["summary"] and body.reviewed_summary is None:
            raise tasks.TaskError(
                "summary_review",
                "Review the existing summary before carrying it into a message branch.",
            )
        privacy = dict(settings["settings"]["privacy"])
        for identity in prefix:
            source = db.execute(
                "SELECT p.memory_disabled,p.harness_disabled FROM messages m "
                "JOIN message_privacy p ON p.message_id=m.id WHERE m.id=? AND m.session_id NOT IN "
                "(SELECT session_id FROM forgotten_sessions)",
                (identity,),
            ).fetchone()
            if source is None:
                raise tasks.TaskError(
                    "source_unavailable", "A source is forgotten or its privacy is unknown."
                )
            privacy["memoryDisabled"] |= bool(source[0])
            privacy["harnessDisabled"] |= bool(source[1])
        child = "ses_" + uuid.uuid4().hex[:16]
        db.execute(
            "INSERT INTO sessions(id,parent_id,title,status,created_at,summary) "
            "VALUES(?,?,?,'open',?,?)",
            (child, sid, parent["title"], time.time(), body.reviewed_summary),
        )
        carried_settings = {**settings["settings"], "privacy": privacy}
        db.execute(
            "INSERT INTO session_settings VALUES(?,1,?) ON CONFLICT(session_id) "
            "DO UPDATE SET revision=1,settings=excluded.settings",
            (child, json.dumps(carried_settings)),
        )
        for position, identity in enumerate(prefix):
            db.execute(
                "INSERT INTO context_inherited_messages VALUES(?,?,?)", (child, identity, position)
            )
        if policy:
            value = json.loads(policy[1])
            value["messagePolicies"] = {
                key: mode for key, mode in value["messagePolicies"].items() if key in prefix
            }
            db.execute("INSERT INTO context_policies VALUES(?,1,?)", (child, json.dumps(value)))
        db.execute(
            "INSERT INTO message_fork_requests VALUES(?,?,?,?,?)",
            (body.request_id, sid, message_id, child, digest),
        )
        db.commit()
        return {
            "session_id": child,
            "parent_id": sid,
            "message_id": message_id,
            "replayed": False,
            "privacy": privacy,
        }


def router(get_store, owner):
    api = APIRouter(dependencies=[Depends(owner)])

    @api.post("/sessions/{sid}/messages/{message_id}/fork")
    def fork(sid: str, message_id: str, body: Fork):
        return create(get_store(), sid, message_id, body)

    @api.get("/sessions/{sid}/branch-history")
    def history(sid: str, limit: int = Query(100, ge=1, le=200), cursor: str | None = None):
        store = get_store()
        with store._connect() as db:
            tasks._source(db, sid)
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT message_id FROM context_inherited_messages WHERE session_id=? "
                    "ORDER BY position",
                    (sid,),
                )
            ]
            inherited = set(ids)
            ids += [
                row[0]
                for row in db.execute(
                    "SELECT id FROM messages WHERE session_id=? ORDER BY seq", (sid,)
                )
            ]
        if cursor is not None and cursor not in ids:
            raise tasks.TaskError("invalid_cursor", "Message cursor unavailable.", 422)
        start = ids.index(cursor) + 1 if cursor else 0
        selected = ids[start : start + limit]
        return {
            "results": [
                {**store.get_message(identity), "inherited": identity in inherited}
                for identity in selected
            ],
            "next_cursor": selected[-1] if start + limit < len(ids) else None,
        }

    return api
