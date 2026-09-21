"""Read-only recovery evidence through ToolGate, bound to a saved Pi action."""

import math
import re
import time

from . import system_actions as actions
from .system_port_reviews import _mappings, exchange

STEP_NAMES = {
    "stop",
    "snapshot",
    "retire",
    "rename",
    "disconnect",
    "create",
    "connect",
    "start",
    "verify",
}


def _container(value, expected, *, replacement=False):
    if value is None:
        return None
    cid, presence = value["containerId"], value["presence"]
    if replacement and presence == "identity_unconfirmed" and cid is None and expected is None:
        return {"containerId": None, "presence": presence}
    if cid != expected or not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{64}", cid):
        raise ValueError("container mismatch")
    if presence in ("missing", "unavailable"):
        return {"containerId": cid, "presence": presence}
    if (
        presence != "present"
        or not isinstance(value.get("image"), str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["image"])
        or value.get("status")
        not in ("created", "running", "paused", "restarting", "removing", "exited", "dead")
        or any(
            type(value.get(key)) is not bool for key in ("running", "paused", "restarting", "dead")
        )
    ):
        raise ValueError("invalid container observation")
    binding_status = value["bindingsStatus"]
    if binding_status not in ("observed", "configured", "unavailable"):
        raise ValueError("invalid bindings status")
    bindings = _mappings(value["bindings"]) if binding_status != "unavailable" else None
    result = {
        key: value[key]
        for key in (
            "containerId",
            "presence",
            "image",
            "status",
            "running",
            "paused",
            "restarting",
            "dead",
            "bindingsStatus",
        )
    }
    return {**result, "bindings": bindings}


def project(value, action_id, container_id):
    if (
        value["actionId"] != action_id
        or value["state"] not in ("completed", "dispatching", "outcome_unknown")
        or value.get("canResume") is not False
        or value.get("canReleaseReservation") is not False
        or value.get("inspection")
        not in ("observed", "partial", "unavailable", "not_required", "in_progress")
    ):
        raise ValueError("recovery mismatch")
    raw_steps = value["steps"]
    if not isinstance(raw_steps, list) or len(raw_steps) > 150:
        raise ValueError("invalid steps")
    steps, candidate = [], None
    for ordinal, step in enumerate(raw_steps):
        if (
            type(step["ordinal"]) is not int
            or step["ordinal"] != ordinal
            or step["name"] not in STEP_NAMES
            or step["status"] not in ("observed", "dispatching", "outcome_unknown")
        ):
            raise ValueError("invalid step")
        reference = step["reference"]
        if reference is not None and (
            not isinstance(reference, str)
            or not re.fullmatch(
                r"sha256:[a-f0-9]{64}" if step["name"] == "snapshot" else r"[a-f0-9]{64}", reference
            )
        ):
            raise ValueError("invalid reference")
        if step["name"] == "create" and step["status"] == "observed":
            if candidate is not None or reference is None or reference == container_id:
                raise ValueError("invalid replacement identity")
            candidate = reference
        steps.append({key: step[key] for key in ("ordinal", "name", "status", "reference")})
    stamp = value["observedAt"]
    if stamp is not None and (
        type(stamp) not in (int, float)
        or not math.isfinite(stamp)
        or stamp <= 0
        or stamp > time.time() + 30
    ):
        raise ValueError("invalid observation time")
    match = value.get("replacementBindingsMatch")
    if match is not None and type(match) is not bool:
        raise ValueError("invalid match")
    return {
        "actionId": action_id,
        "state": value["state"],
        "steps": steps,
        "source": _container(value["source"], container_id),
        "replacement": _container(value["replacement"], candidate, replacement=True),
        "observedAt": stamp,
        "inspection": value["inspection"],
        "replacementBindingsMatch": match,
        "canResume": False,
        "canReleaseReservation": False,
    }


def inspect(store, gate, identity, *, transport=None):
    actions._configured(gate)
    with store._connect() as db:
        row = actions._row(db, identity)
        if not actions._port(row):
            raise actions.ActionError(
                "not_port_action", "This action has no port recovery record.", 404
            )
    action_id = "pi_system_" + identity
    try:
        value = exchange(gate, "/v2/agent/system/port-recovery/" + action_id, transport=transport)
        return project(value, action_id, row["container_id"])
    except Exception:
        raise actions.ActionError(
            "recovery_unavailable", "Replacement recovery evidence is unavailable.", 503
        ) from None
