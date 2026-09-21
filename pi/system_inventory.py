"""Owner inventory receipts through ToolGate; Pi has no direct host access."""

import json
import time
from datetime import UTC, datetime

from pydantic import Field

from .agents import StrictModel
from .toolgate import ApprovalRequired, ToolPending, ToolRefused, ToolResult

TOOL = "system.inventory"
SCHEMA = """
CREATE TABLE IF NOT EXISTS system_inventory_reads (
 id TEXT PRIMARY KEY, row_limit INTEGER NOT NULL, state TEXT NOT NULL,
 approval TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


class Read(StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    limit: int = Field(default=100, ge=1, le=200)


class InventoryError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _row(db, identity):
    row = db.execute("SELECT * FROM system_inventory_reads WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise InventoryError("not_found", "Inventory request unavailable.", 404)
    return row


def _view(row):
    return {
        "requestId": row["id"],
        "actionId": "pi_inventory_" + row["id"],
        "state": row["state"],
        "limit": row["row_limit"],
        "approval": json.loads(row["approval"]) if row["approval"] else None,
        "errorCode": row["error_code"],
        "inventory": None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "source": "toolgate/system.inventory",
        "refreshRequiresNewRequest": True,
    }


def _configured(gate):
    if gate is None:
        raise InventoryError("unconfigured", "ToolGate inventory access is not configured.", 503)


def _projection(value, limit):
    # ToolGate owns full inventory validation; this boundary additionally refuses
    # a differently shaped receipt or any claim that this path permits mutation.
    if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False)) > 1024 * 1024:
        raise ValueError("invalid inventory")
    if value.get("mode") != "observed" or value.get("status") not in (
        "ok",
        "partial",
        "unavailable",
    ):
        raise ValueError("invalid inventory")
    capabilities = value.get("capabilities", {})
    if (
        not isinstance(capabilities, dict)
        or capabilities.get("inspection") is not True
        or any(
            capabilities.get(key) is not False
            for key in (
                "processActions",
                "containerActions",
                "portMutation",
                "terminal",
                "files",
            )
        )
    ):
        raise ValueError("unsupported capability")
    for section in ("processes", "containers", "ports"):
        rows = value[section]["results"]
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("inventory limit")
    sampled = datetime.fromisoformat(value["sampledAt"])
    if sampled.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return max(0, (datetime.now(UTC) - sampled).total_seconds())


def _record(store, identity, outcome):
    inventory, age, approval, code = None, None, None, None
    if isinstance(outcome, ApprovalRequired):
        state = "awaiting_approval"
        approval = json.dumps({"requestId": outcome.request_id, "expiresAt": outcome.expires_at})
    elif isinstance(outcome, ToolPending):
        state = "unknown"
    elif isinstance(outcome, ToolResult) and outcome.tool_id == TOOL and outcome.ok is True:
        try:
            age = _projection(outcome.result, 200)
            inventory, state = outcome.result, "complete"
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            state, code = "failed", "invalid_inventory"
    else:
        state, code = "failed", "read_failed"
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        if inventory is not None:
            try:
                age = _projection(inventory, row["row_limit"])
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                inventory, age, state, code = None, None, "failed", "invalid_inventory"
        # Losing access to a known receipt does not erase the earlier completion.
        if state == "unknown" and row["state"] in ("complete", "failed"):
            result = _view(row)
            result["receiptStatus"] = "unavailable"
        else:
            db.execute(
                "UPDATE system_inventory_reads SET state=?,approval=?,error_code=?,updated_at=? "
                "WHERE id=?",
                (state, approval, code, time.time(), identity),
            )
            result = _view(_row(db, identity))
        db.commit()
    return {**result, "inventory": inventory, "currentAgeSeconds": age}


def _dispatch(store, gate, identity, limit, approval=None):
    try:
        outcome = gate.invoke(
            TOOL,
            {"limit": limit},
            action_id="pi_inventory_" + identity,
            approval_request_id=approval,
        )
    except ToolRefused:
        outcome = ToolResult(False, None, TOOL)
    except Exception:
        outcome = ToolPending(
            "outcome_unknown", "Read receipt unavailable", "pi_inventory_" + identity
        )
    return _record(store, identity, outcome)


def request(store, gate, body):
    _configured(gate)
    body = Read.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM system_inventory_reads WHERE id=?", (body.request_id,)
        ).fetchone()
        if row:
            if row["row_limit"] != body.limit:
                raise InventoryError(
                    "request_conflict", "Request ID is already bound to another limit."
                )
            return _view(row)
        now = time.time()
        db.execute(
            "INSERT INTO system_inventory_reads VALUES (?,?,'dispatching',NULL,NULL,?,?)",
            (body.request_id, body.limit, now, now),
        )
        db.commit()
    return _dispatch(store, gate, body.request_id, body.limit)


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
            "UPDATE system_inventory_reads SET state='dispatching',updated_at=? WHERE id=?",
            (time.time(), identity),
        )
        db.commit()
    return _dispatch(store, gate, identity, row["row_limit"], approval)


def recover(store):
    with store._connect() as db:
        return db.execute(
            "UPDATE system_inventory_reads SET state='unknown',updated_at=? "
            "WHERE state='dispatching'",
            (time.time(),),
        ).rowcount
