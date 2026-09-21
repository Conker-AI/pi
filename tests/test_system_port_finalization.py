import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import system_actions as actions
from pi import system_actions_api
from pi import system_port_finalization as recovery
from pi.toolgate import ToolGateClient
from tests.test_system_port_recovery import Stream, saved  # noqa: F401
from tests.test_system_ports import BODY, observation


def run(store, value, *, approval=None, status=200):
    def handle(request):
        assert request.method == "POST"
        assert request.url.path == "/v2/agent/system/port-finalizations"
        body = json.loads(request.content)
        assert body["action_id"] == "pi_system_" + BODY.request_id
        assert body.get("approval_request_id") == approval
        return httpx.Response(status, stream=Stream(json.dumps(value).encode()))

    return recovery.finalize(
        store,
        ToolGateClient("http://synthetic", "synthetic"),
        BODY.request_id,
        recovery.Request(approval_request_id=approval),
        transport=httpx.MockTransport(handle),
    )


def receipt():
    return {
        "code": "OK",
        "status": "completed",
        "action_id": "pi_system_" + BODY.request_id,
        "result": {"ok": True, "result": observation()},
    }


def test_recovery_approval_does_not_replace_original_ledger(request):
    store = request.getfixturevalue("saved")
    before = actions.history(store)
    result = run(
        store,
        {
            "code": "CONFIRMATION_REQUIRED",
            "request_id": "approval",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        },
    )
    assert result["recoveryApproval"]["requestId"] == "approval"
    assert actions.history(store) == before


def test_recovered_receipt_updates_original_action_and_strips_private_fields(request):
    store = request.getfixturevalue("saved")
    result = run(store, receipt(), approval="approval")
    assert result["state"] == "complete"
    assert len(actions.history(store)) == 1
    assert "private-value" not in json.dumps(result)
    assert run(store, receipt(), approval="approval")["state"] == "complete"


@pytest.mark.parametrize("change", ["identity", "target", "status", "failed", "malformed"])
def test_invalid_receipt_preserves_unknown_state(request, change):
    store = request.getfixturevalue("saved")
    value = receipt()
    if change == "identity":
        value["action_id"] = "another"
    elif change == "target":
        value["result"]["result"]["containerId"] = "e" * 64
    elif change == "status":
        value["status"] = "outcome_unknown"
    elif change == "failed":
        value["result"]["ok"] = False
    else:
        value = []
    with pytest.raises(actions.ActionError):
        run(store, value)
    assert actions.history(store)[0]["state"] == "unknown"


def test_transport_failure_keeps_unknown_for_reconciliation(request):
    store = request.getfixturevalue("saved")
    with pytest.raises(actions.ActionError):
        run(store, {"error": "private-value"}, status=503)
    assert actions.history(store)[0]["state"] == "unknown"


def test_owner_route_requires_auth_and_returns_no_store(request, monkeypatch):
    store = request.getfixturevalue("saved")
    calls = []

    def authorize(x_owner: str = Header(default="")):
        if x_owner != "synthetic-owner":
            raise HTTPException(401)

    def finalize(*args):
        calls.append(args[2])
        return {"state": "complete"}

    monkeypatch.setattr(recovery, "finalize", finalize)
    app = FastAPI()
    app.include_router(system_actions_api.router(lambda: store, lambda: None, authorize))
    client = TestClient(app)
    path = f"/system/actions/{BODY.request_id}/recovery/finalize"
    assert client.post(path, json={}).status_code == 401
    assert not calls
    response = client.post(path, json={}, headers={"x-owner": "synthetic-owner"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls == [BODY.request_id]
