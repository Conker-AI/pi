"""Durable owner system actions; only ToolGate can authorize or execute effects."""

import json
import time
from typing import Literal

from pydantic import Field

from .agents import StrictModel
from .toolgate import ApprovalRequired, ToolPending, ToolRefused, ToolResult

TOOL = "system.container-control"
SCHEMA = """
CREATE TABLE IF NOT EXISTS system_actions (
 id TEXT PRIMARY KEY, container_id TEXT NOT NULL, action TEXT NOT NULL,
 state TEXT NOT NULL, approval TEXT, error_code TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


class Request(StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Literal["start", "stop", "restart"]


class ActionError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _configured(gate):
    if gate is None:
        raise ActionError("unconfigured", "ToolGate system control is not configured.", 503)


def _row(db, identity):
    row = db.execute("SELECT * FROM system_actions WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ActionError("not_found", "System action unavailable.", 404)
    return row


def _args(row):
    return {"container_id": row["container_id"], "action": row["action"]}


def _view(row):
    return {
        "requestId": row["id"],
        "actionId": "pi_system_" + row["id"],
        "containerId": row["container_id"],
        "action": row["action"],
        "state": row["state"],
        "approval": json.loads(row["approval"]) if row["approval"] else None,
        "errorCode": row["error_code"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "observation": None,
        "source": "toolgate/system.container-control",
    }


def _observation(value, row):
    if (
        not isinstance(value, dict)
        or value.get("containerId") != row["container_id"]
        or value.get("action") != row["action"]
        or value.get("outcome") != "observed"
        or value.get("dispatched") is not True
    ):
        raise ValueError("receipt mismatch")
    result = {
        "containerId": row["container_id"],
        "action": row["action"],
        "outcome": "observed",
        "dispatched": True,
    }
    for name in ("before", "after"):
        state = value.get(name)
        if not isinstance(state, dict) or state.get("status") not in (
            "created",
            "running",
            "paused",
            "restarting",
            "removing",
            "exited",
            "dead",
        ):
            raise ValueError("invalid observation")
        result[name] = {"status": state["status"]}
        for flag in ("running", "paused", "restarting", "dead"):
            if type(state.get(flag)) is not bool:
                raise ValueError("invalid observation")
            result[name][flag] = state[flag]
    return result


def _record(store, identity, outcome):
    observation, approval, code = None, None, None
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        state = "unknown"
        if isinstance(outcome, ApprovalRequired):
            if (
                outcome.tool_id == TOOL
                and outcome.args == _args(row)
                and isinstance(outcome.request_id, str)
                and 1 <= len(outcome.request_id) <= 200
            ):
                state = "awaiting_approval"
                approval = json.dumps(
                    {"requestId": outcome.request_id, "expiresAt": outcome.expires_at}
                )
            else:
                code = "invalid_approval"
        elif isinstance(outcome, ToolResult) and outcome.tool_id == TOOL:
            if outcome.ok is False:
                state, code = "failed", "action_refused"
            elif outcome.ok is True:
                try:
                    observation = _observation(outcome.result, row)
                    state = "complete"
                except (ValueError, TypeError, KeyError):
                    code = "invalid_receipt"
        # A late poll cannot roll a known outcome back to pending/approval.
        if state in ("unknown", "awaiting_approval") and row["state"] in ("complete", "failed"):
            result = {**_view(row), "receiptStatus": "unavailable"}
        else:
            db.execute(
                "UPDATE system_actions SET state=?,approval=?,error_code=?,updated_at=? WHERE id=?",
                (state, approval, code, time.time(), identity),
            )
            result = _view(_row(db, identity))
        db.commit()
    return {**result, "observation": observation}


def _dispatch(store, gate, row, approval=None):
    try:
        result = gate.invoke(
            TOOL, _args(row), action_id="pi_system_" + row["id"], approval_request_id=approval
        )
    except ToolRefused:
        result = ToolResult(False, None, TOOL)
    except Exception:
        result = ToolPending(
            "outcome_unknown", "Action receipt unavailable", "pi_system_" + row["id"]
        )
    return _record(store, row["id"], result)


def request(store, gate, body):
    _configured(gate)
    body = Request.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM system_actions WHERE id=?", (body.request_id,)).fetchone()
        if row:
            if _args(row) != {"container_id": body.container_id, "action": body.action}:
                raise ActionError(
                    "request_conflict", "Request ID is bound to another system action."
                )
            return _view(row)
        now = time.time()
        db.execute(
            "INSERT INTO system_actions VALUES (?,?,?,'dispatching',NULL,NULL,?,?)",
            (body.request_id, body.container_id, body.action, now, now),
        )
        row = _row(db, body.request_id)
        db.commit()
    return _dispatch(store, gate, row)


def inspect(store, gate, identity):
    with store._connect() as db:
        row = _row(db, identity)
    if row["state"] in ("awaiting_approval", "dispatching"):
        return _view(row)
    _configured(gate)
    try:
        outcome = gate.check_action("pi_system_" + identity, TOOL)
    except Exception:
        outcome = ToolPending("outcome_unknown", "Receipt unavailable", "pi_system_" + identity)
    return _record(store, identity, outcome)


def resume(store, gate, identity):
    _configured(gate)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        if row["state"] != "awaiting_approval":
            return _view(row)
        approval = json.loads(row["approval"])["requestId"]
        db.execute(
            "UPDATE system_actions SET state='dispatching',updated_at=? WHERE id=?",
            (time.time(), identity),
        )
        db.commit()
    return _dispatch(store, gate, row, approval)


def history(store, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ActionError("invalid_limit", "Use a history limit from 1 to 100.", 422)
    with store._connect() as db:
        return [
            _view(row)
            for row in db.execute(
                "SELECT * FROM system_actions ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)
            )
        ]


def recover(store):
    with store._connect() as db:
        return db.execute(
            "UPDATE system_actions SET state='unknown',updated_at=? WHERE state='dispatching'",
            (time.time(),),
        ).rowcount
