"""Read only configured lifecycle targets, without turning them into live inventory."""

import json
import re
import time

import httpx

from .system_actions import SERVICE_PATTERN, ActionError


def read(gate, *, transport=None, services=False):
    if gate is None:
        raise ActionError("unconfigured", "ToolGate system control is not configured.", 503)
    try:
        path = "/v2/agent/system/services" if services else "/v2/agent/system/targets"
        source = "toolgate/process-control" if services else "toolgate/container-control"
        field = "services" if services else "containers"
        pattern = SERVICE_PATTERN if services else r"[a-f0-9]{64}"
        deadline = time.monotonic() + 10
        with (
            httpx.Client(
                transport=transport, trust_env=False, follow_redirects=False, timeout=5
            ) as client,
            client.stream(
                "GET",
                gate.base_url + path,
                headers={**gate._headers(), "Accept-Encoding": "identity"},
            ) as response,
        ):
            if response.status_code != 200:
                raise ValueError("unavailable")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("encoding")
            raw = bytearray()
            for chunk in response.iter_raw():
                if (
                    len(raw) + len(chunk) > (600_000 if services else 200_000)
                    or time.monotonic() > deadline
                ):
                    raise ValueError("limit")
                raw.extend(chunk)
            if time.monotonic() > deadline:
                raise ValueError("deadline")
        value = json.loads(raw)
        status = value["status"]
        if (
            value.get("kind") != "configured-targets"
            or value.get("observed") is not False
            or value.get("requiresApproval") is not True
            or value.get("source") != source
            or status
            not in (
                "configured",
                "disabled",
                "locked_down",
                "not_configured",
                "invalid_configuration",
            )
        ):
            raise ValueError("invalid capabilities")
        containers, actions = value[field], value["actions"]
        if (
            not isinstance(containers, list)
            or len(containers) > 2000
            or any(
                not isinstance(item, str) or len(item) > 260 or not re.fullmatch(pattern, item)
                for item in containers
            )
            or len(set(containers)) != len(containers)
            or actions != (["start", "stop", "restart"] if status == "configured" else [])
            or (status != "configured" and containers)
        ):
            raise ValueError("invalid targets")
        return {
            "kind": "configured-targets",
            "status": status,
            field: containers,
            "actions": actions,
            "requiresApproval": True,
            "observed": False,
            "source": source,
        }
    except Exception:
        raise ActionError(
            "targets_unavailable", "Managed target information is unavailable.", 503
        ) from None
