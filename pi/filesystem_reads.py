"""Durable directory receipts through ToolGate; Pi has no direct file access."""

import json
import time
from datetime import UTC, datetime

from pydantic import Field, field_validator

from .agents import StrictModel
from .toolgate import ApprovalRequired, ToolPending, ToolRefused, ToolResult

TOOL = "system.files-list"
SCHEMA = """
CREATE TABLE IF NOT EXISTS filesystem_reads (
 id TEXT PRIMARY KEY, root_id TEXT NOT NULL, path TEXT NOT NULL, row_limit INTEGER NOT NULL,
 state TEXT NOT NULL,
 approval TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


class Read(StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    root_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    path: str = ""
    limit: int = Field(default=200, ge=1, le=200)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        if not _relative(value):
            raise ValueError("Use a safe relative directory path.")
        return value


class FileReadError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = code
        self.status, self.detail = status, {"code": code, "message": message}


def _row(db, identity):
    row = db.execute("SELECT * FROM filesystem_reads WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise FileReadError("not_found", "Directory request unavailable.", 404)
    return row


def _view(row):
    return {
        "requestId": row["id"],
        "actionId": "pi_files_" + row["id"],
        "state": row["state"],
        "limit": row["row_limit"],
        "rootId": row["root_id"],
        "path": row["path"],
        "approval": json.loads(row["approval"]) if row["approval"] else None,
        "errorCode": row["error_code"],
        "listing": None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "source": "toolgate/system.files-list",
        "refreshRequiresNewRequest": True,
    }


def _configured(gate):
    if gate is None:
        raise FileReadError("unconfigured", "ToolGate listing access is not configured.", 503)


def _component(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value not in (".", "..")
        and not any(
            c in "/\\" or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value
        )
    )


def _relative(value):
    return (
        isinstance(value, str)
        and len(value) <= 4096
        and (value == "" or all(_component(c) for c in value.split("/")))
    )


def _projection(value, root_id, path, limit):
    if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False)) > 1024 * 1024:
        raise ValueError("Invalid directory receipt")
    if value.get("mode") != "observed" or value.get("rootId") != root_id:
        raise ValueError("Invalid root")
    if value.get("path") != path or type(value.get("truncated")) is not bool:
        raise ValueError("Invalid directory")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > limit:
        raise ValueError("Invalid entries")
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not _component(entry.get("name")):
            raise ValueError("Invalid entry name")
        name = entry["name"]
        expected = f"{path}/{name}" if path else name
        if entry.get("path") != expected or not _relative(expected) or name in names:
            raise ValueError("Invalid entry path")
        if entry.get("kind") not in ("directory", "file", "symlink", "other"):
            raise ValueError("Invalid entry kind")
        names.add(name)
    sampled = datetime.fromisoformat(value["sampledAt"])
    if sampled.tzinfo is None or sampled.utcoffset() is None:
        raise ValueError("Timestamp lacks timezone")
    clean = {key: value[key] for key in ("mode", "rootId", "path", "truncated", "sampledAt")}
    clean["entries"] = [{key: entry[key] for key in ("name", "path", "kind")} for entry in entries]
    return clean, max(0, (datetime.now(UTC) - sampled).total_seconds())


def _record(store, identity, outcome):
    listing, age, approval, code = None, None, None, None
    if isinstance(outcome, ApprovalRequired):
        state = "awaiting_approval"
        approval = json.dumps({"requestId": outcome.request_id, "expiresAt": outcome.expires_at})
    elif isinstance(outcome, ToolPending):
        state = "unknown"
    elif isinstance(outcome, ToolResult) and outcome.tool_id == TOOL and outcome.ok is True:
        listing, state = outcome.result, "complete"
    else:
        state, code = "failed", "read_failed"
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        if isinstance(outcome, ApprovalRequired) and (
            outcome.tool_id != TOOL
            or outcome.args
            != {"root_id": row["root_id"], "path": row["path"], "limit": row["row_limit"]}
        ):
            state, approval, code = "failed", None, "invalid_approval"
        if state == "complete":
            try:
                listing, age = _projection(listing, row["root_id"], row["path"], row["row_limit"])
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                listing, age, state, code = None, None, "failed", "invalid_listing"
        # Losing access to a known receipt does not erase the earlier completion.
        if state == "unknown" and row["state"] in ("complete", "failed"):
            result = _view(row)
            result["receiptStatus"] = "unavailable"
        else:
            db.execute(
                "UPDATE filesystem_reads SET state=?,approval=?,error_code=?,updated_at=? "
                "WHERE id=?",
                (state, approval, code, time.time(), identity),
            )
            result = _view(_row(db, identity))
        db.commit()
    return {**result, "listing": listing, "currentAgeSeconds": age}


def _dispatch(store, gate, identity, root_id, path, limit, approval=None):
    try:
        outcome = gate.invoke(
            TOOL,
            {"root_id": root_id, "path": path, "limit": limit},
            action_id="pi_files_" + identity,
            approval_request_id=approval,
        )
    except ToolRefused:
        outcome = ToolResult(False, None, TOOL)
    except Exception:
        outcome = ToolPending("outcome_unknown", "Read receipt unavailable", "pi_files_" + identity)
    return _record(store, identity, outcome)


def request(store, gate, body):
    _configured(gate)
    body = Read.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM filesystem_reads WHERE id=?", (body.request_id,)).fetchone()
        if row:
            if (row["root_id"], row["path"], row["row_limit"]) != (
                body.root_id,
                body.path,
                body.limit,
            ):
                raise FileReadError(
                    "request_conflict", "Request ID is already bound to another directory request."
                )
            return _view(row)
        now = time.time()
        db.execute(
            "INSERT INTO filesystem_reads VALUES (?,?,?,?,'dispatching',NULL,NULL,?,?)",
            (body.request_id, body.root_id, body.path, body.limit, now, now),
        )
        db.commit()
    return _dispatch(store, gate, body.request_id, body.root_id, body.path, body.limit)


def inspect(store, gate, identity):
    with store._connect() as db:
        row = _row(db, identity)
        view = _view(row)
    if row["state"] in ("awaiting_approval", "dispatching"):
        return view
    _configured(gate)
    try:
        outcome = gate.check_action(view["actionId"], TOOL)
    except Exception:
        outcome = ToolPending("outcome_unknown", "Read receipt unavailable", view["actionId"])
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
            "UPDATE filesystem_reads SET state='dispatching',updated_at=? WHERE id=?",
            (time.time(), identity),
        )
        db.commit()
    return _dispatch(store, gate, identity, row["root_id"], row["path"], row["row_limit"], approval)


def recover(store):
    with store._connect() as db:
        return db.execute(
            "UPDATE filesystem_reads SET state='unknown',updated_at=? WHERE state='dispatching'",
            (time.time(),),
        ).rowcount
