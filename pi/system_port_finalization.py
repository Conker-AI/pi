"""Owner-reviewed recovery of a saved port action; never dispatches Docker."""

from datetime import UTC, datetime

from pydantic import Field

from . import system_actions as actions
from .agents import StrictModel
from .system_port_reviews import exchange
from .toolgate import ToolResult


class Request(StrictModel):
    approval_request_id: str | None = Field(default=None, min_length=1, max_length=160)


class GateRequest(Request):
    action_id: str


def finalize(store, gate, identity, body, *, transport=None):
    actions._configured(gate)
    body = Request.model_validate(body.model_dump())
    with store._connect() as db:
        row = actions._row(db, identity)
    if not actions._port(row):
        raise actions.ActionError("invalid_action", "Recovery requires a saved port action.", 422)
    if row["state"] not in ("unknown", "complete"):
        raise actions.ActionError("invalid_state", "Inspect the original action before recovery.")
    action_id = "pi_system_" + identity
    try:
        value = exchange(
            gate,
            "/v2/agent/system/port-finalizations",
            request=GateRequest(action_id=action_id, **body.model_dump()),
            transport=transport,
        )
        if value.get("code") == "CONFIRMATION_REQUIRED":
            rid, expires = value["request_id"], value["expires_at"]
            if not isinstance(rid, str) or not 1 <= len(rid) <= 160 or not isinstance(expires, str):
                raise ValueError()
            expiry = datetime.fromisoformat(expires)
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError()
            # Recovery approval is distinct from the original execution approval;
            # it remains discoverable in ToolGate's durable owner Inbox.
            return {
                "requestId": identity,
                "actionId": action_id,
                "state": "awaiting_approval",
                "recoveryApproval": {"requestId": rid, "expiresAt": expiry.isoformat()},
            }
        if (
            value.get("code") != "OK"
            or value.get("status") != "completed"
            or value.get("action_id") != action_id
            or value["result"].get("ok") is not True
        ):
            raise ValueError()
        observation = actions._port_observation(value["result"]["result"], row)
        if observation["outcome"] != "observed":
            raise ValueError()
    except Exception:
        raise actions.ActionError(
            "recovery_unavailable",
            "Recovery receipt unavailable. Inspect the original action before retrying.",
            503,
        ) from None
    return actions._record(store, identity, ToolResult(True, observation, actions.PORT_TOOL))
