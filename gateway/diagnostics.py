"""One secret-free diagnostic contract for the owner UI and host CLI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 256 * 1024

RECOVERY = {
    "owner-login": ("Configure owner sign-in", None, "conker auth setup"),
    "runtime": ("Restart the local services", "/system", "conker restart"),
    "owner-channel": ("Repair the approval channel", "/system", "conker restart"),
    "store": ("Inspect Pi storage", "/system", "conker logs pi"),
    "memory": ("Inspect memory", "/memory", "conker logs memorygate"),
    "local-model": ("Check the local answer model", "/settings", "conker logs ollama"),
    "hosted-model": ("Check hosted model settings", "/settings", None),
    "actions": ("Inspect tool approvals", "/tools", "conker logs toolgate"),
}


def _read_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("unavailable response")
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("invalid response")
    return value


def _probe(client: httpx.Client, url: str, header: str, key: str) -> dict[str, Any] | None:
    if not key:
        return None
    try:
        return _read_json(client.get(url, headers={header: key}, timeout=3))
    except (httpx.HTTPError, ValueError, TypeError):
        return None


def _finding(
    identity: str,
    area: str,
    label: str,
    raw_status: object,
    *,
    optional: bool = False,
) -> dict[str, Any]:
    status = raw_status if isinstance(raw_status, str) and len(raw_status) <= 60 else "unknown"
    if optional and status == "not_configured":
        state, detail, recovery = "optional", "Not configured; this capability is optional.", None
    elif status in {"ok", "ready", "busy"}:
        state, detail, recovery = "ok", "Working normally.", None
    else:
        action = RECOVERY[identity]
        state, detail = (
            "attention",
            {
                "not_configured": "Required host configuration is missing.",
                "unavailable": "The service could not be reached.",
                "degraded": "The service reported degraded health.",
            }.get(status, "The service returned an unrecognized health state."),
        )
        recovery = {"label": action[0], "uiRoute": action[1], "command": action[2]}
    return {
        "id": identity,
        "area": area,
        "label": label,
        "status": state,
        "observedStatus": status,
        "detail": detail,
        "recovery": recovery,
    }


def collect_diagnostics(config, auth, client: httpx.Client) -> dict[str, Any]:
    """Probe fixed internal endpoints and project only the stable owner contract."""
    pi = _probe(
        client,
        config.pi_url.rstrip("/") + "/health",
        "X-Pi-Gateway-Key",
        config.pi_key,
    )
    owner = _probe(
        client,
        config.toolgate_url.rstrip("/") + "/v2/owner/requests",
        "X-ToolGate-Owner-Key",
        config.owner_key,
    )
    checks = pi.get("checks", {}) if pi and isinstance(pi.get("checks"), dict) else {}
    findings = [
        _finding(
            "owner-login",
            "access",
            "Owner sign-in",
            "ok" if auth.configured() else "not_configured",
        ),
        _finding(
            "runtime",
            "services",
            "Conker runtime",
            pi.get("status", "unknown") if pi else "unavailable",
        ),
        _finding(
            "owner-channel",
            "access",
            "Approval control",
            "ok"
            if owner is not None
            else ("not_configured" if not config.owner_key else "unavailable"),
        ),
        _finding("store", "services", "Conversation storage", _check(checks, "store")),
        _finding("memory", "services", "Memory", _check(checks, "memory"), optional=True),
        _finding("local-model", "models", "Local answer model", _check(checks, "local_provider")),
        _finding(
            "hosted-model",
            "models",
            "Hosted answer model",
            _check(checks, "hosted_provider"),
            optional=True,
        ),
        _finding(
            "actions",
            "services",
            "Tool approvals",
            _check(checks, "action_boundary"),
            optional=True,
        ),
    ]
    attention = sum(item["status"] == "attention" for item in findings)
    return {
        "schemaVersion": 1,
        "status": "attention" if attention else "ok",
        "generatedAt": datetime.now(UTC).isoformat(),
        "summary": {
            "attention": attention,
            "ok": sum(item["status"] == "ok" for item in findings),
            "optional": sum(item["status"] == "optional" for item in findings),
        },
        "findings": findings,
    }


def _check(checks: dict[str, Any], name: str) -> object:
    value = checks.get(name)
    return value.get("status", "unknown") if isinstance(value, dict) else "unknown"
