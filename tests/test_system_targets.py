import json

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import system_actions_api, system_targets
from pi.system_actions import ActionError
from pi.toolgate import ToolGateClient


def configured():
    return {
        "kind": "configured-targets",
        "status": "configured",
        "containers": ["a" * 64],
        "actions": ["start", "stop", "restart"],
        "requiresApproval": True,
        "observed": False,
        "source": "toolgate/container-control",
    }


class Stream(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        yield self.data


def transport(data, calls=None, status=200):
    def handle(request):
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, stream=Stream(json.dumps(data).encode()))

    return httpx.MockTransport(handle)


def test_read_projects_bounded_configuration_only():
    calls = []
    gate = ToolGateClient("http://gate.test", "synthetic-key")
    result = system_targets.read(
        gate, transport=transport({**configured(), "socket": "/secret"}, calls)
    )
    assert result == configured()
    assert calls[0].method == "GET" and calls[0].url.path == "/v2/agent/system/targets"
    assert calls[0].headers["X-ToolGate-Execution-Key"] == "synthetic-key"


@pytest.mark.parametrize(
    "change",
    [
        {"observed": True},
        {"requiresApproval": False},
        {"containers": ["../daemon"]},
        {"containers": ["a" * 64] * 2001},
        {"status": "disabled"},
        {"actions": ["delete"]},
        {"extra": "x" * 200_000},
    ],
)
def test_invalid_or_oversized_catalogue_never_enables_controls(change):
    with pytest.raises(ActionError, match="unavailable"):
        system_targets.read(
            ToolGateClient("http://gate.test", "key"),
            transport=transport({**configured(), **change}),
        )


def test_unconfigured_and_failed_gate_are_not_empty_success():
    with pytest.raises(ActionError, match="not configured"):
        system_targets.read(None)
    with pytest.raises(ActionError, match="unavailable"):
        system_targets.read(
            ToolGateClient("http://gate.test", "key"),
            transport=transport({"error": "secret"}, status=403),
        )
    value = {**configured(), "status": "not_configured", "actions": [], "containers": []}
    assert (
        system_targets.read(ToolGateClient("http://gate.test", "key"), transport=transport(value))
        == value
    )


def test_owner_route_is_read_only_and_not_cached(monkeypatch):
    calls = []

    def authorize(x_owner: str | None = Header(None)):
        if x_owner != "owner":
            raise HTTPException(401)

    monkeypatch.setattr(system_targets, "read", lambda gate: calls.append(gate) or configured())
    app = FastAPI()
    app.include_router(system_actions_api.targets_router(lambda: "scoped-gate", authorize))
    with TestClient(app) as client:
        assert client.get("/system/targets").status_code == 401
        assert calls == []
        result = client.get("/system/targets", headers={"X-Owner": "owner"})
        assert result.json() == configured() and result.headers["cache-control"] == "no-store"
        assert calls == ["scoped-gate"]
