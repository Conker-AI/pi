"""Owner context policy, immutable per-turn policy and explicit budget conflicts."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PolicyName = Literal["keep-exact", "allow-summary", "retrieve", "exclude"]


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Budget(Strict):
    contextWindowTokens: int = Field(ge=2, le=10000000)
    outputReserveTokens: int = Field(ge=1)
    otherInputTokens: int = Field(ge=0)

    @model_validator(mode="after")
    def reserve(self):
        if self.outputReserveTokens >= self.contextWindowTokens:
            raise ValueError("Output reserve must be smaller than the context window.")
        return self


class Policy(Strict):
    sessionInstructions: str = Field(max_length=16000)
    messagePolicies: dict[str, PolicyName]
    budget: Budget

    @field_validator("messagePolicies")
    @classmethod
    def selectors(cls, value):
        if len(value) > 10000 or any(not key.strip() or len(key) > 200 for key in value):
            raise ValueError("Use bounded message identities.")
        return value


class Update(Strict):
    expected_revision: int = Field(ge=0)
    policy: Policy


class ReviewedFork(Strict):
    expected_revision: int = Field(ge=1)
    expected_last_message_id: str | None = Field(default=None, max_length=200)
    summary: str = Field(max_length=16000)
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


SCHEMA = """
CREATE TABLE IF NOT EXISTS context_inherited_messages (
 session_id TEXT NOT NULL REFERENCES sessions(id),
 message_id TEXT NOT NULL REFERENCES messages(id), position INTEGER NOT NULL,
 PRIMARY KEY(session_id,message_id)
);
CREATE TABLE IF NOT EXISTS context_fork_requests (
 request_id TEXT PRIMARY KEY, source_session TEXT NOT NULL REFERENCES sessions(id),
 child_session TEXT NOT NULL REFERENCES sessions(id), payload_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_policies (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), revision INTEGER NOT NULL,
 policy TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_context_policies (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), revision INTEGER NOT NULL, policy TEXT
);
CREATE TRIGGER IF NOT EXISTS capture_turn_context_policy AFTER INSERT ON turns
BEGIN
 INSERT INTO turn_context_policies(turn_id,revision,policy)
 SELECT NEW.id,COALESCE(p.revision,0),p.policy
 FROM sessions s LEFT JOIN context_policies p ON p.session_id=s.id WHERE s.id=NEW.session_id;
END;
CREATE TRIGGER IF NOT EXISTS turn_context_policy_no_update BEFORE UPDATE ON turn_context_policies
BEGIN SELECT RAISE(ABORT,'turn context policy is immutable'); END;
CREATE TRIGGER IF NOT EXISTS turn_context_policy_no_replace BEFORE INSERT ON turn_context_policies
WHEN EXISTS(SELECT 1 FROM turn_context_policies WHERE turn_id=NEW.turn_id)
BEGIN SELECT RAISE(ABORT,'turn context policy is immutable'); END;
CREATE TRIGGER IF NOT EXISTS turn_context_policy_no_delete BEFORE DELETE ON turn_context_policies
BEGIN SELECT RAISE(ABORT,'turn context policy is immutable'); END;
"""


class ContextError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _session(db, identity):
    row = db.execute("SELECT status FROM sessions WHERE id=?", (identity,)).fetchone()
    if row is None or row[0] == "forgotten":
        raise ContextError("unavailable", "Conversation unavailable.", 404)


def load(store, identity, turn_id=None, request_id=None):
    if turn_id:
        with store._connect() as db:
            while retry := db.execute(
                "SELECT source_turn_id FROM response_retries WHERE turn_id=? AND session_id=?",
                (turn_id, identity),
            ).fetchone():
                turn_id = retry[0]
    if turn_id or request_id:
        from . import context_retrieval

        frozen = context_retrieval.read(store, identity, turn_id=turn_id, request_id=request_id)
        if frozen is not None:
            return {"revision": frozen["revision"], "policy": frozen["policy"]}
    with store._connect() as db:
        _session(db, identity)
        if turn_id:
            row = db.execute(
                "SELECT p.revision,p.policy FROM turn_context_policies p "
                "JOIN turns t ON t.id=p.turn_id WHERE t.id=? AND t.session_id=?",
                (turn_id, identity),
            ).fetchone()
            # Pre-migration turns used ordinary full history; never apply new policy retroactively.
        else:
            row = db.execute(
                "SELECT revision,policy FROM context_policies WHERE session_id=?", (identity,)
            ).fetchone()
        return {
            "revision": row[0] if row else 0,
            "policy": json.loads(row[1]) if row and row[1] else None,
        }


def save(store, identity, body: Update):
    body = Update.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _session(db, identity)
        row = db.execute(
            "SELECT revision FROM context_policies WHERE session_id=?", (identity,)
        ).fetchone()
        revision = row[0] if row else 0
        if revision != body.expected_revision:
            raise ContextError("revision_conflict", "Context policy changed; reload before saving.")
        for message_id in body.policy.messagePolicies:
            inactive = db.execute(
                "SELECT 1 FROM response_versions v JOIN response_families f "
                "ON f.root_message_id=v.root_message_id WHERE v.message_id=? "
                "AND f.session_id=? AND f.selected_message_id!=v.message_id",
                (message_id, identity),
            ).fetchone()
            if inactive:
                raise ContextError(
                    "inactive_version",
                    "Select this response version before setting its context rule.",
                )
            message = db.execute(
                "SELECT session_id FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            inherited = db.execute(
                "SELECT 1 FROM context_inherited_messages WHERE session_id=? AND message_id=?",
                (identity, message_id),
            ).fetchone()
            if message is None or (message[0] != identity and not inherited):
                raise ContextError(
                    "outside_boundary", "Message does not belong to this conversation.", 422
                )
        db.execute(
            "INSERT INTO context_policies VALUES (?,?,?) ON CONFLICT(session_id) "
            "DO UPDATE SET revision=excluded.revision,policy=excluded.policy",
            (identity, revision + 1, body.policy.model_dump_json()),
        )
        db.commit()
    return {"revision": revision + 1, "policy": body.policy.model_dump()}


def estimate(text):
    return math.ceil(len(text.encode("utf-8")) / 4) + 4 if text else 0


def select_history(policy, rows, retrieved_ids=None):
    """No silent compaction. Retrieve needs a separate authorized selection result."""
    if not policy:
        return rows
    value = Policy.model_validate(policy)
    by_id = {row["id"]: row for row in rows}
    for identity, mode in value.messagePolicies.items():
        if identity not in by_id:
            raise ContextError("missing_message", "Context refers to an unavailable message.")
        if mode == "retrieve" and retrieved_ids is None:
            raise ContextError(
                "retrieval_pending", "Resolve retrieval before dispatch or choose another policy."
            )
        if mode == "keep-exact" and (
            by_id[identity].get("redacted") or by_id[identity].get("content_status") == "forgotten"
        ):
            raise ContextError("unavailable_pin", "An exact pin was redacted; review context.")
    return [
        row
        for row in rows
        if not row.get("redacted")
        and row.get("content_status") != "forgotten"
        and value.messagePolicies.get(row["id"]) != "exclude"
        and (
            value.messagePolicies.get(row["id"]) != "retrieve" or row["id"] in (retrieved_ids or [])
        )
    ]


def check_budget(policy, messages):
    if not policy:
        return
    value = Policy.model_validate(policy)
    count = (
        sum(estimate(message.content) + 4096 * len(message.images) for message in messages)
        + value.budget.otherInputTokens
        + value.budget.outputReserveTokens
    )
    if count > value.budget.contextWindowTokens:
        raise ContextError(
            "budget_overflow",
            "Estimated context exceeds the window; exact pins and instructions were not dropped.",
        )


def redact(db, session_ids):
    from . import context_retrieval

    context_retrieval.redact(db, session_ids)
    # Called only by the exclusive offline forgetting transaction.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='context_policies'").fetchone():
        return
    db.execute("DROP TRIGGER IF EXISTS turn_context_policy_no_update")
    for identity in session_ids:
        db.execute("DELETE FROM context_policies WHERE session_id=?", (identity,))
        db.execute(
            "UPDATE turn_context_policies SET policy=NULL WHERE turn_id IN "
            "(SELECT id FROM turns WHERE session_id=?)",
            (identity,),
        )
    db.execute(
        "CREATE TRIGGER turn_context_policy_no_update BEFORE UPDATE ON turn_context_policies "
        "BEGIN SELECT RAISE(ABORT,'turn context policy is immutable'); END;"
    )


def history(store, identity):
    """Resolve inherited exact pins by original identity, never transcript copies."""
    with store._connect() as db:
        inherited = db.execute(
            "SELECT message_id FROM context_inherited_messages "
            "WHERE session_id=? ORDER BY position",
            (identity,),
        ).fetchall()
    rows = []
    for item in inherited:
        row = store.get_message(item[0])
        if row is None or row.get("content_status") == "forgotten":
            raise ContextError("unavailable_pin", "An inherited source was forgotten.")
        rows.append(row)
    from . import response_versions

    own = store.messages(identity)
    with store._connect() as db:
        return rows + response_versions.project(db, identity, own)


def reviewed_fork(store, identity, body: ReviewedFork):
    body = ReviewedFork.model_validate(body.model_dump())
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _session(db, identity)
        previous = db.execute(
            "SELECT * FROM context_fork_requests WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if previous:
            if previous["source_session"] != identity or previous["payload_hash"] != digest:
                raise ContextError("request_conflict", "Fork request identity was reused.")
            _session(db, previous["child_session"])
            return {
                "session_id": previous["child_session"],
                "parent_id": identity,
                "replayed": True,
            }
        parent = db.execute("SELECT * FROM sessions WHERE id=?", (identity,)).fetchone()
        if parent["status"] != "open":
            raise ContextError("session_closed", "Fork an open conversation.")
        pending = db.execute(
            "SELECT 1 FROM turns WHERE session_id=? AND (status NOT IN "
            "('complete','failed','interrupted','cancelled') "
            "OR (status='interrupted' AND acted=1)) LIMIT 1",
            (identity,),
        ).fetchone()
        if (
            pending
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                (identity,),
            ).fetchone()
        ):
            raise ContextError("turn_pending", "Finish the current turn before reviewing a fork.")
        last = db.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY seq DESC LIMIT 1", (identity,)
        ).fetchone()
        if (last[0] if last else None) != body.expected_last_message_id:
            raise ContextError("boundary_changed", "Conversation changed after the fork review.")
        current = db.execute(
            "SELECT * FROM context_policies WHERE session_id=?", (identity,)
        ).fetchone()
        if current is None or current["revision"] != body.expected_revision:
            raise ContextError("revision_conflict", "Context changed after the fork review.")
        policy = Policy.model_validate_json(current["policy"])
        pins = [key for key, value in policy.messagePolicies.items() if value == "keep-exact"]
        if "retrieve" in policy.messagePolicies.values():
            raise ContextError("retrieval_pending", "Resolve retrieval before approving a summary.")
        pin_chars = 0
        for message_id in pins:
            row = db.execute(
                "SELECT m.content FROM messages m LEFT JOIN forgotten_sessions f "
                "ON f.session_id=m.session_id WHERE m.id=? AND f.session_id IS NULL",
                (message_id,),
            ).fetchone()
            if row is None:
                raise ContextError("unavailable_pin", "Exact source unavailable.")
            pin_chars += estimate(str(json.loads(row[0])))
        cost = pin_chars + estimate(policy.sessionInstructions) + estimate(body.summary)
        if (
            cost + policy.budget.otherInputTokens + policy.budget.outputReserveTokens
            > policy.budget.contextWindowTokens
        ):
            raise ContextError(
                "budget_overflow", "Pins, instructions and summary exceed the estimated budget."
            )
        child, now = "ses_" + uuid.uuid4().hex[:16], time.time()
        db.execute(
            "INSERT INTO sessions(id,parent_id,title,status,created_at,summary) "
            "VALUES (?,?,?,'open',?,?)",
            (child, identity, parent["title"], now, body.summary),
        )
        carried = policy.model_copy(update={"messagePolicies": {key: "keep-exact" for key in pins}})
        db.execute(
            "INSERT INTO context_policies VALUES (?,1,?)", (child, carried.model_dump_json())
        )
        for position, message_id in enumerate(pins):
            db.execute(
                "INSERT INTO context_inherited_messages VALUES (?,?,?)",
                (child, message_id, position),
            )
        db.execute("UPDATE sessions SET status='forked',closed_at=? WHERE id=?", (now, identity))
        db.execute(
            "INSERT INTO context_fork_requests VALUES (?,?,?,?)",
            (body.request_id, identity, child, digest),
        )
        db.commit()
    return {"session_id": child, "parent_id": identity, "replayed": False}
