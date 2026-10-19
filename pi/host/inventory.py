"""Owner inventory receipts through ToolGate; Pi has no direct host access."""

import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime

from pi.actions.toolgate import ApprovalRequired, ToolPending, ToolRefused, ToolResult
from pi.core.errors import ServiceError
from pi.host.inventory_models import (
    ConfiguredTarget,
    ConfiguredTargetsView,
    ContainerSectionView,
    ContainerView,
    InventoryObservation,
    InventoryView,
    ObservedEnvelope,
    PortSectionView,
    PortView,
    ProcessSectionView,
    ProcessView,
    Read,
    SourceScopeView,
)

TOOL = "system.inventory"
SCHEMA = """
CREATE TABLE IF NOT EXISTS system_inventory_reads (
 id TEXT PRIMARY KEY, row_limit INTEGER NOT NULL, state TEXT NOT NULL,
 approval TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS system_inventory_identity (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret BLOB NOT NULL CHECK(length(secret)=32)
);
"""


class InventoryError(ServiceError, RuntimeError):
    pass


def initialize(db):
    if db.execute("SELECT 1 FROM system_inventory_identity WHERE singleton=1").fetchone() is None:
        db.execute("INSERT INTO system_inventory_identity VALUES (1,?)", (secrets.token_bytes(32),))


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
    if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False)) > 1024 * 1024:
        raise ValueError("invalid inventory")
    observed = ObservedEnvelope.model_validate(value)
    for section in ("processes", "containers", "ports"):
        if len(getattr(observed, section).results) > limit:
            raise ValueError("inventory limit")
    sampled = datetime.fromisoformat(observed.sampledAt)
    return observed.model_dump(), max(0, (datetime.now(UTC) - sampled).total_seconds())


def _record(store, identity, outcome):
    inventory, age, approval, code = None, None, None, None
    if isinstance(outcome, ApprovalRequired):
        state = "awaiting_approval"
        approval = json.dumps({"requestId": outcome.request_id, "expiresAt": outcome.expires_at})
    elif isinstance(outcome, ToolPending):
        state = "unknown"
    elif isinstance(outcome, ToolResult) and outcome.tool_id == TOOL and outcome.ok is True:
        try:
            inventory, age = _projection(outcome.result, 200)
            state = "complete"
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            state, code = "failed", "invalid_inventory"
    else:
        state, code = "failed", "read_failed"
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        if inventory is not None:
            try:
                inventory, age = _projection(inventory, row["row_limit"])
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


def _identity_key(store):
    with store._connect() as db:
        row = db.execute(
            "SELECT secret FROM system_inventory_identity WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise InventoryError(
                "identity_unavailable", "Inventory identity protection is unavailable.", 503
            )
        return bytes(row["secret"])


def _opaque(kind, identity, key):
    return f"{kind}_{hmac.new(key, identity.encode(), hashlib.sha256).hexdigest()}"


def _section(section, rows):
    return {
        "status": section["status"],
        "truncated": section["truncated"],
        "errors": section["errors"],
        "results": rows,
    }


def _address_scope(value):
    if value in {"127.0.0.1", "::1"}:
        return "loopback"
    if value in {"0.0.0.0", "::"}:
        return "all-interfaces"
    return "specific"


def browser_observation(value, key):
    observed = ObservedEnvelope.model_validate(value)
    process_ids = {row.id: _opaque("process", row.id, key) for row in observed.processes.results}
    container_ids = {
        row.id: _opaque("container", row.id, key) for row in observed.containers.results
    }
    processes = [
        ProcessView(
            id=process_ids[row.id],
            name=row.name,
            status=row.status,
            createdAt=row.createdAt,
            memoryBytes=row.memoryBytes,
        )
        for row in observed.processes.results
    ]
    containers = [
        ContainerView(
            id=container_ids[row.id],
            name=row.name,
            status=row.status,
            imageConfigured=bool(row.image),
        )
        for row in observed.containers.results
    ]
    ports = [
        PortView(
            id=_opaque("port", row.id, key),
            kind=row.kind,
            addressScope=_address_scope(row.hostAddress),
            hostPort=row.hostPort,
            targetPort=row.targetPort,
            protocol=row.protocol,
            processId=process_ids.get(row.processId),
            containerId=container_ids.get(row.containerId),
            state=(
                "declared-binding"
                if row.kind == "container-binding"
                else "listening"
                if row.protocol == "tcp"
                else "bound"
            ),
        )
        for row in observed.ports.results
    ]
    return InventoryObservation(
        mode="observed",
        status=observed.status,
        sampledAt=observed.sampledAt,
        ageSeconds=observed.ageSeconds,
        collectionSeconds=observed.collectionSeconds,
        sourceScopes=SourceScopeView(
            process=observed.source.processScope,
            network=observed.source.networkScope,
            containers=observed.source.containerScope,
        ),
        processes=ProcessSectionView(**_section(observed.processes.model_dump(), processes)),
        containers=ContainerSectionView(**_section(observed.containers.model_dump(), containers)),
        ports=PortSectionView(**_section(observed.ports.model_dump(), ports)),
        unavailableFieldCount=len(observed.unavailableFields),
        capabilities=observed.capabilities,
    )


def browser_view(store, value):
    observation = (
        browser_observation(value["inventory"], _identity_key(store))
        if value.get("inventory")
        else None
    )
    return InventoryView(
        requestId=value["requestId"],
        state=value["state"],
        limit=value["limit"],
        approvalRequired=value["state"] == "awaiting_approval",
        errorCode=value.get("errorCode"),
        observation=observation,
        receiptStatus=value.get("receiptStatus"),
        currentAgeSeconds=value.get("currentAgeSeconds"),
        createdAt=value["createdAt"],
        updatedAt=value["updatedAt"],
        source=value["source"],
        refreshRequiresNewRequest=value["refreshRequiresNewRequest"],
        contentIncluded=observation is not None,
    )


def configured_targets(store, value, kind):
    field = "services" if kind == "services" else "containers"
    prefix = "service" if kind == "services" else "container"
    key = _identity_key(store)
    results = [
        ConfiguredTarget(
            id=_opaque(prefix, identity, key),
            kind=prefix,
            name=identity if kind == "services" else None,
        )
        for identity in value[field]
    ]
    return ConfiguredTargetsView(
        kind=kind,
        status=value["status"],
        results=results,
        requiresApproval=value["requiresApproval"],
        observed=value["observed"],
        contentIncluded=bool(results),
    )


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
