"""Answer versions share one chronological context slot; evidence remains immutable."""

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from . import submissions, tasks

SCHEMA = """
CREATE TABLE IF NOT EXISTS response_families (
 root_message_id TEXT PRIMARY KEY REFERENCES messages(id),
 session_id TEXT NOT NULL REFERENCES sessions(id),
 selected_message_id TEXT NOT NULL REFERENCES messages(id),
 revision INTEGER NOT NULL CHECK(revision>=1)
);
CREATE TABLE IF NOT EXISTS response_versions (
 message_id TEXT PRIMARY KEY REFERENCES messages(id),
 root_message_id TEXT NOT NULL REFERENCES response_families(root_message_id),
 retry_of TEXT REFERENCES messages(id)
);
CREATE TRIGGER IF NOT EXISTS response_version_no_update BEFORE UPDATE ON response_versions
BEGIN SELECT RAISE(ABORT,'response version identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS response_version_no_delete BEFORE DELETE ON response_versions
BEGIN SELECT RAISE(ABORT,'response version identity is permanent'); END;
CREATE TRIGGER IF NOT EXISTS response_version_no_replace BEFORE INSERT ON response_versions
WHEN EXISTS(SELECT 1 FROM response_versions WHERE message_id=NEW.message_id)
BEGIN SELECT RAISE(ABORT,'response version identity is permanent'); END;
"""


def _final(db, identity):
    row = db.execute(
        "SELECT m.session_id,t.status FROM messages m JOIN turn_messages tm ON tm.message_id=m.id "
        "JOIN turns t ON t.id=tm.turn_id WHERE m.id=? AND tm.purpose='final' "
        "AND m.session_id NOT IN (SELECT session_id FROM forgotten_sessions)",
        (identity,),
    ).fetchone()
    if row is None or row["status"] != "complete":
        raise tasks.TaskError("version_unavailable", "Choose a completed response version.", 404)
    return row


def register(db, original, response):
    """Called inside retry completion's transaction, after the final turn is complete."""
    source, result = _final(db, original), _final(db, response)
    if source["session_id"] != result["session_id"] or original == response:
        raise tasks.TaskError("version_scope", "Response versions must share a conversation.")
    previous = db.execute(
        "SELECT * FROM response_versions WHERE message_id=?", (response,)
    ).fetchone()
    if previous:
        if previous["retry_of"] != original:
            raise tasks.TaskError("version_conflict", "Response already belongs to another retry.")
        return previous["root_message_id"]
    prior = db.execute(
        "SELECT root_message_id FROM response_versions WHERE message_id=?", (original,)
    ).fetchone()
    root = prior[0] if prior else original
    if not prior:
        db.execute(
            "INSERT INTO response_families VALUES(?,?,?,1)", (root, source["session_id"], root)
        )
        db.execute("INSERT INTO response_versions VALUES(?,?,NULL)", (root, root))
    db.execute("INSERT INTO response_versions VALUES(?,?,?)", (response, root, original))
    return root


def project(db, sid, rows):
    """Replace only local families; inherited branch references stay exactly frozen."""
    families = db.execute("SELECT * FROM response_families WHERE session_id=?", (sid,)).fetchall()
    selected = {row["root_message_id"]: row["selected_message_id"] for row in families}
    versions = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT v.message_id,v.root_message_id FROM response_versions v "
            "JOIN response_families f ON f.root_message_id=v.root_message_id WHERE f.session_id=?",
            (sid,),
        )
    }
    by_id = {row["id"]: row for row in rows}
    result = []
    for row in rows:
        identity = row["id"]
        root = versions.get(identity)
        if root is None:
            result.append(row)
        elif root == identity:
            chosen = by_id.get(selected[root])
            if chosen is None:
                raise tasks.TaskError(
                    "version_unavailable", "The selected response is unavailable."
                )
            result.append(chosen)
    return result


def _view(db, sid, root):
    tasks._source(db, sid)
    family = db.execute(
        "SELECT * FROM response_families WHERE session_id=? AND root_message_id=?", (sid, root)
    ).fetchone()
    if family is None:
        raise tasks.TaskError("not_found", "Response family not found.", 404)
    versions = db.execute(
        "SELECT v.message_id,v.retry_of,tm.turn_id FROM response_versions v "
        "JOIN messages m ON m.id=v.message_id JOIN turn_messages tm ON tm.message_id=m.id "
        "WHERE v.root_message_id=? ORDER BY m.seq",
        (root,),
    ).fetchall()
    return {**dict(family), "versions": [dict(row) for row in versions]}


class Selection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    message_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


def activate(db, sid, root, identity):
    """Completion auto-selects unless it would silently drop an exact context rule."""
    policy = db.execute("SELECT policy FROM context_policies WHERE session_id=?", (sid,)).fetchone()
    rules = json.loads(policy[0])["messagePolicies"] if policy else {}
    ids = [
        row[0]
        for row in db.execute(
            "SELECT message_id FROM response_versions WHERE root_message_id=?", (root,)
        )
    ]
    if identity not in ids or any(mid in rules and mid != identity for mid in ids):
        return False
    db.execute(
        "UPDATE response_families SET selected_message_id=?,revision=revision+1 "
        "WHERE root_message_id=? AND selected_message_id!=?",
        (identity, root, identity),
    )
    return True


def select(store, sid, root, body):
    body = Selection.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        view = _view(db, sid, root)
        if view["revision"] != body.expected_revision:
            raise tasks.TaskError("revision_conflict", "Response selection changed; reload first.")
        if body.message_id not in {v["message_id"] for v in view["versions"]}:
            raise tasks.TaskError("foreign_version", "Choose a version from this response family.")
        if (
            submissions._busy(db, sid)
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                (sid,),
            ).fetchone()
        ):
            raise tasks.TaskError(
                "session_busy", "Wait for current work before selecting a response."
            )
        if view["selected_message_id"] != body.message_id:
            policy = db.execute(
                "SELECT policy FROM context_policies WHERE session_id=?", (sid,)
            ).fetchone()
            rules = json.loads(policy[0])["messagePolicies"] if policy else {}
            # A policy refers to exact evidence IDs, never an interchangeable family.
            if any(
                v["message_id"] in rules and v["message_id"] != body.message_id
                for v in view["versions"]
            ):
                raise tasks.TaskError(
                    "version_context_conflict",
                    "Review context rules for the previous version first.",
                )
            _final(db, body.message_id)
            db.execute(
                "UPDATE response_families SET selected_message_id=?,revision=revision+1 "
                "WHERE root_message_id=?",
                (body.message_id, root),
            )
        result = _view(db, sid, root)
        db.commit()
        return result


def router(get_store, owner):
    api = APIRouter(dependencies=[Depends(owner)])

    @api.get("/sessions/{sid}/response-families/{root}")
    def read(sid: str, root: str):
        with get_store()._connect() as db:
            return _view(db, sid, root)

    @api.post("/sessions/{sid}/response-families/{root}/select")
    def choose(sid: str, root: str, body: Selection):
        return select(get_store(), sid, root, body)

    return api
