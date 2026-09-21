"""Owner context policy, immutable per-turn policy and explicit budget conflicts."""

from __future__ import annotations

import json
import math
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


SCHEMA = """
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


def load(store, identity, turn_id=None):
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
            message = db.execute(
                "SELECT session_id FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if message is None or message[0] != identity:
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


def select_history(policy, rows):
    """No silent compaction. Retrieve needs a separate authorized selection result."""
    if not policy:
        return rows
    value = Policy.model_validate(policy)
    by_id = {row["id"]: row for row in rows}
    for identity, mode in value.messagePolicies.items():
        if identity not in by_id:
            raise ContextError("missing_message", "Context refers to an unavailable message.")
        if mode == "retrieve":
            raise ContextError(
                "retrieval_pending", "Resolve retrieval before dispatch or choose another policy."
            )
        if mode == "keep-exact" and by_id[identity].get("redacted"):
            raise ContextError("unavailable_pin", "An exact pin was redacted; review context.")
    return [
        row
        for row in rows
        if not row.get("redacted") and value.messagePolicies.get(row["id"]) != "exclude"
    ]


def check_budget(policy, messages):
    if not policy:
        return
    value = Policy.model_validate(policy)
    count = (
        sum(estimate(message.content) for message in messages)
        + value.budget.otherInputTokens
        + value.budget.outputReserveTokens
    )
    if count > value.budget.contextWindowTokens:
        raise ContextError(
            "budget_overflow",
            "Estimated context exceeds the window; exact pins and instructions were not dropped.",
        )


def redact(db, session_ids):
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
