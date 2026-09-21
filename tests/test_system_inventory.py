from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import system_inventory as inventory
from pi import system_inventory_api
from pi.store import Store
from pi.toolgate import ApprovalRequired, ToolGateClient, ToolPending, ToolResult


def observed():
    return {
        "mode": "observed",
        "status": "partial",
        "sampledAt": datetime.now(UTC).isoformat(),
        "processes": {"results": []},
        "containers": {"results": []},
        "ports": {"results": []},
        "capabilities": {
            "inspection": True,
            "processActions": False,
            "containerActions": False,
            "portMutation": False,
            "terminal": False,
            "files": False,
        },
    }


class Gate:
    def __init__(self):
        self.invocations, self.checks = [], []
        self.result = ToolResult(True, observed(), inventory.TOOL)

    def invoke(self, tool, args, **kwargs):
        self.invocations.append((tool, args, kwargs))
        return self.result

    def check_action(self, action_id, tool):
        self.checks.append((action_id, tool))
        return self.result


def test_concurrent_request_and_restart_only_inspect_saved_action(tmp_path):
    path, gate = tmp_path / "pi.db", Gate()
    body = inventory.Read(request_id="inventory_request_01", limit=12)
    with closing(Store(path)) as store:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: inventory.request(store, gate, body), range(2)))
        assert len(gate.invocations) == 1
        assert gate.invocations[0][:2] == (inventory.TOOL, {"limit": 12})
        with pytest.raises(inventory.InventoryError, match="bound"):
            inventory.request(store, gate, body.model_copy(update={"limit": 13}))
    with closing(Store(path)) as store:
        result = inventory.inspect(store, gate, body.request_id)
        assert result["state"] == "complete" and result["inventory"]["status"] == "partial"
        assert result["currentAgeSeconds"] >= 0
        assert len(gate.invocations) == len(gate.checks) == 1
        with store._connect() as db:
            record = str(dict(db.execute("SELECT * FROM system_inventory_reads").fetchone()))
            assert "sampledAt" not in record  # Host observations remain in ToolGate's receipt.


def test_approval_is_not_recreated_and_resumes_exact_saved_request(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ApprovalRequired(
            "approval-one", None, "Review", inventory.TOOL, {"limit": 100}
        )
        body = inventory.Read(request_id="inventory_approval_01")
        first = inventory.request(store, gate, body)
        assert first["state"] == "awaiting_approval"
        inventory.request(store, gate, body)
        inventory.inspect(store, gate, body.request_id)
        assert len(gate.invocations) == 1 and not gate.checks
        gate.result = ToolResult(True, observed(), inventory.TOOL)
        assert inventory.resume(store, gate, body.request_id)["state"] == "complete"
        assert gate.invocations[-1][2]["approval_request_id"] == "approval-one"
        inventory.resume(store, gate, body.request_id)
        assert len(gate.invocations) == 2


def test_unknown_recovery_never_dispatches_and_keeps_known_completion(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolPending("outcome_unknown", "Timeout", "action")
        body = inventory.Read(request_id="inventory_unknown_01")
        assert inventory.request(store, gate, body)["state"] == "unknown"
        gate.result = ToolResult(True, observed(), inventory.TOOL)
        assert inventory.inspect(store, gate, body.request_id)["state"] == "complete"
        gate.result = ToolPending("outcome_unknown", "Offline", "action")
        result = inventory.inspect(store, gate, body.request_id)
        assert result["state"] == "complete" and result["receiptStatus"] == "unavailable"
        assert result["inventory"] is None and len(gate.invocations) == 1
        with store._connect() as db:
            db.execute("UPDATE system_inventory_reads SET state='dispatching'")
        assert inventory.recover(store) == 1
        assert inventory.inspect(store, gate, body.request_id)["state"] == "unknown"
        assert len(gate.invocations) == 1


def test_owner_api_real_client_and_no_direct_systemgate_transport(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        requests = []

        def send(url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(
                200,
                json={
                    "code": "OK",
                    "status": "completed",
                    "action_id": kwargs["json"]["action_id"],
                    "result": {"ok": True, "result": observed()},
                },
            )

        monkeypatch.setattr(httpx, "post", send)
        gate = ToolGateClient("http://gate.test", "synthetic-scoped-key")

        def authorize(x_owner: str | None = Header(None)):
            if x_owner != "owner":
                raise HTTPException(401)

        app = FastAPI()
        app.include_router(system_inventory_api.router(lambda: store, lambda: gate, authorize))
        with TestClient(app) as client:
            body = {"request_id": "inventory_api_request"}
            assert client.post("/system/inventory", json=body).status_code == 401
            assert not requests
            response = client.post("/system/inventory", json=body, headers={"X-Owner": "owner"})
            assert response.json()["state"] == "complete"
            assert requests[0][0] == "http://gate.test/v2/tools/system.inventory/invoke"
            assert "synthetic-scoped-key" not in response.text


@pytest.mark.parametrize("change", ["mode", "capabilities", "sampledAt"])
def test_malformed_success_is_not_rendered_as_inventory(tmp_path, change):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        value = observed()
        value[change] = "invalid"
        gate.result = ToolResult(True, value, inventory.TOOL)
        result = inventory.request(store, gate, inventory.Read(request_id="invalid_inventory_01"))
        assert result["state"] == "failed" and result["inventory"] is None


def test_unconfigured_gate_does_not_reserve_or_fake_a_sample(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        with pytest.raises(inventory.InventoryError, match="not configured"):
            inventory.request(store, None, inventory.Read(request_id="unconfigured_read_01"))
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM system_inventory_reads").fetchone()[0] == 0
