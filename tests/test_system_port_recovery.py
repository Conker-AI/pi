import json
import time
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import system_actions as actions
from pi import system_actions_api
from pi import system_port_recovery as recovery
from pi.store import Store
from pi.toolgate import ToolGateClient, ToolPending
from tests.test_system_ports import BODY, CID, NEW, Gate


def evidence():
    state = {
        "containerId": NEW,
        "presence": "present",
        "image": "sha256:" + "d" * 64,
        "status": "created",
        "running": False,
        "paused": False,
        "restarting": False,
        "dead": False,
        "bindings": [],
        "bindingsStatus": "configured",
        "Env": "secret",
    }
    return {
        "actionId": "pi_system_" + BODY.request_id,
        "state": "outcome_unknown",
        "inspection": "observed",
        "observedAt": time.time(),
        "canResume": False,
        "canReleaseReservation": False,
        "replacementBindingsMatch": True,
        "source": {"containerId": CID, "presence": "missing", "private": "secret"},
        "replacement": state,
        "steps": [
            {"ordinal": 0, "name": "create", "status": "observed", "reference": NEW},
            {"ordinal": 1, "name": "start", "status": "outcome_unknown", "reference": None},
        ],
    }


class Stream(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        yield self.data


@pytest.fixture
def saved(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolPending("outcome_unknown", "pending", "action")
        actions.request(store, gate, BODY)
        yield store


def test_recovery_uses_fixed_get_and_preserves_local_action(saved):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, stream=Stream(json.dumps(evidence()).encode()))

    before = actions.history(saved)
    result = recovery.inspect(
        saved,
        ToolGateClient("http://gate", "synthetic-key"),
        BODY.request_id,
        transport=httpx.MockTransport(handle),
    )
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path == "/v2/agent/system/port-recovery/pi_system_" + BODY.request_id
    assert seen[0].headers["X-ToolGate-Execution-Key"] == "synthetic-key"
    assert "secret" not in json.dumps(result)
    assert not result["canResume"] and not result["canReleaseReservation"]
    assert actions.history(saved) == before


@pytest.mark.parametrize("change", ["action", "source", "replacement", "resume", "step", "time"])
def test_invalid_evidence_is_not_rendered_as_authority(saved, change):
    value = evidence()
    if change == "action":
        value["actionId"] = "other"
    elif change == "source":
        value["source"]["containerId"] = NEW
    elif change == "replacement":
        value["replacement"]["containerId"] = CID
    elif change == "resume":
        value["canResume"] = True
    elif change == "step":
        value["steps"][0]["reference"] = "shell-command"
    else:
        value["observedAt"] = float("inf")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream(json.dumps(value).encode()))
    )
    with pytest.raises(actions.ActionError) as error:
        recovery.inspect(
            saved, ToolGateClient("http://gate", "key"), BODY.request_id, transport=transport
        )
    assert "secret" not in str(error.value)


def test_owner_recovery_route_is_no_store(saved, monkeypatch):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream(json.dumps(evidence()).encode()))
    )
    original = recovery.inspect
    monkeypatch.setattr(recovery, "inspect", lambda *args: original(*args, transport=transport))
    app = FastAPI()

    def owner(x_owner: str | None = Header(None)):
        if x_owner != "owner":
            raise HTTPException(401)

    app.include_router(
        system_actions_api.router(
            lambda: saved, lambda: ToolGateClient("http://gate", "key"), owner
        )
    )
    client = TestClient(app)
    path = "/system/actions/" + BODY.request_id + "/recovery"
    assert client.get(path).status_code == 401
    response = client.get(path, headers={"x-owner": "owner"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
