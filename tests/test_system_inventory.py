import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import api, system_inventory_api, system_targets
from pi import system_inventory as inventory
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store
from pi.system_actions import ActionError
from pi.toolgate import ApprovalRequired, ToolGateClient, ToolPending, ToolResult

OWNER = "inventory-owner-control-key-" + "o" * 32


def observed():
    section = {"status": "ok", "results": [], "truncated": False, "errors": []}
    return {
        "mode": "observed",
        "status": "partial",
        "sampledAt": datetime.now(UTC).isoformat(),
        "ageSeconds": 0.0,
        "collectionSeconds": 0.01,
        "source": {
            "procfs": "/proc",
            "processScope": "collector-namespace",
            "networkScope": "collector-namespace",
            "containerScope": "configured-docker-daemon",
        },
        "processes": dict(section),
        "containers": {
            "status": "unavailable",
            "results": [],
            "truncated": False,
            "errors": ["collection_failed"],
        },
        "ports": dict(section),
        "capabilities": {
            "inspection": True,
            "processActions": False,
            "containerActions": False,
            "portMutation": False,
            "terminal": False,
            "files": False,
        },
        "unavailableFields": ["containers"],
    }


def observed_rows():
    value = observed()
    created = 123.5
    process_id = f"process:4242:{created.hex()}"
    container_id = "d" * 64
    value.update(status="ok", unavailableFields=[])
    value["processes"]["results"] = [
        {
            "id": process_id,
            "pid": 4242,
            "createdAt": created,
            "name": "worker",
            "status": "sleeping",
            "memoryBytes": 4096.0,
            "cpuPercent": None,
            "command": None,
            "user": None,
            "restarts": None,
            "containerId": None,
            "managed": False,
        }
    ]
    value["containers"] = {
        "status": "ok",
        "results": [
            {
                "id": container_id,
                "name": "api",
                "image": "private.registry.local/secret/image:tag",
                "status": "running",
                "processId": None,
                "restarts": None,
                "managed": False,
            }
        ],
        "truncated": False,
        "errors": [],
    }
    value["ports"]["results"] = [
        {
            "id": "listener:" + "a" * 64,
            "kind": "listener",
            "hostAddress": "10.22.33.44",
            "hostPort": 8080,
            "targetPort": None,
            "protocol": "tcp",
            "processId": process_id,
            "containerId": None,
            "listening": True,
            "bound": True,
        },
        {
            "id": "binding:" + "b" * 64,
            "kind": "container-binding",
            "hostAddress": "0.0.0.0",
            "hostPort": 8443,
            "targetPort": 443,
            "protocol": "tcp",
            "processId": None,
            "containerId": container_id,
            "listening": None,
            "bound": None,
        },
    ]
    return value


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


def test_connected_owner_inventory_is_redacted_exact_and_restart_safe(tmp_path, monkeypatch):
    path = tmp_path / "owner-inventory.db"
    store = Store(path)
    gate = Gate()
    gate.result = ToolResult(True, observed_rows(), inventory.TOOL)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", gate, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "inventory-admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(("r" * 32).encode()).hexdigest(),
        raising=False,
    )
    headers = {"X-Pi-Owner-Key": OWNER}
    client = TestClient(api.app)
    body = {"request_id": "owner_inventory_0001", "limit": 10}

    assert client.post("/system/inventory", json=body).status_code == 401
    assert (
        client.post(
            "/system/inventory", json=body, headers={"X-Pi-Gateway-Key": "r" * 32}
        ).status_code
        == 401
    )
    response = client.post("/system/inventory", json=body, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["schemaVersion"] == 1
    assert value["authority"] == "none"
    assert value["execution"] == "read-only-observation"
    assert value["contentIncluded"] is True and value["state"] == "complete"
    observation = value["observation"]
    process = observation["processes"]["results"][0]
    container = observation["containers"]["results"][0]
    ports = observation["ports"]["results"]
    assert process["id"].startswith("process_") and process["name"] == "worker"
    assert container["id"].startswith("container_") and container["imageConfigured"] is True
    assert ports[0]["addressScope"] == "specific"
    assert ports[1]["addressScope"] == "all-interfaces"
    assert ports[0]["processId"] == process["id"]
    assert ports[1]["containerId"] == container["id"]

    def leaves(item):
        if isinstance(item, dict):
            for key, child in item.items():
                yield key
                yield from leaves(child)
        elif isinstance(item, list):
            for child in item:
                yield from leaves(child)
        else:
            yield item

    exposed = list(leaves(value))
    assert 4242 not in exposed
    for secret in ("10.22.33.44", "private.registry.local", "d" * 64, "/proc"):
        assert all(secret not in item for item in exposed if isinstance(item, str))
    assert not {"command", "user"}.intersection(item for item in exposed if isinstance(item, str))
    assert len(gate.invocations) == 1
    assert (
        client.post("/system/inventory?host=other", json=body, headers=headers).status_code == 422
    )
    assert len(gate.invocations) == 1

    replay = client.post("/system/inventory", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["contentIncluded"] is False
    assert len(gate.invocations) == 1

    store.close()
    reopened = Store(path)
    monkeypatch.setattr(api.app.state, "store", reopened, raising=False)
    try:
        read = client.get("/system/inventory/owner_inventory_0001", headers=headers)
        assert read.status_code == 200 and read.json()["state"] == "complete"
        assert read.json()["observation"]["processes"]["results"][0]["id"] == process["id"]
        assert len(gate.checks) == 1 and len(gate.invocations) == 1
    finally:
        reopened.close()


def test_connected_owner_inventory_hides_approval_and_projects_configured_targets(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "inventory-approval.db")
    gate = Gate()
    gate.result = ApprovalRequired(
        "private-approval-reference", 9999999999, "Review", inventory.TOOL, {"limit": 100}
    )
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", gate, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "inventory-admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    headers = {"X-Pi-Owner-Key": OWNER}
    client = TestClient(api.app)
    body = {"request_id": "owner_inventory_approval"}
    pending = client.post("/system/inventory", headers=headers, json=body)
    assert pending.status_code == 200 and pending.json()["approvalRequired"] is True
    assert "private-approval-reference" not in pending.text
    gate.result = ToolResult(True, observed(), inventory.TOOL)
    resumed = client.post("/system/inventory/owner_inventory_approval/resume", headers=headers)
    assert resumed.status_code == 200 and resumed.json()["state"] == "complete"
    assert gate.invocations[-1][2]["approval_request_id"] == "private-approval-reference"

    def targets(_gate, *, services=False, transport=None):
        if services:
            return {
                "kind": "configured-targets",
                "status": "configured",
                "services": ["system:conker-api.service"],
                "actions": ["start", "stop", "restart"],
                "requiresApproval": True,
                "observed": False,
                "source": "toolgate/process-control",
            }
        return {
            "kind": "configured-targets",
            "status": "configured",
            "containers": ["e" * 64],
            "actions": ["start", "stop", "restart"],
            "requiresApproval": True,
            "observed": False,
            "source": "toolgate/container-control",
        }

    monkeypatch.setattr(system_targets, "read", targets)
    services = client.get("/system/inventory/configured/services", headers=headers)
    containers = client.get("/system/inventory/configured/containers", headers=headers)
    assert services.status_code == containers.status_code == 200
    assert services.json()["observed"] is False and services.json()["execution"] == "not-triggered"
    assert services.json()["results"][0]["name"] == "system:conker-api.service"
    assert containers.json()["results"][0]["id"].startswith("container_")
    assert "e" * 64 not in containers.text
    assert "actions" not in services.json() and "actions" not in containers.json()
    assert services.headers["cache-control"] == "no-store"
    assert (
        client.get("/system/inventory/configured/services?path=/proc", headers=headers).status_code
        == 422
    )
    with store._connect() as db:
        identity_before = db.execute(
            "SELECT secret FROM system_inventory_identity WHERE singleton=1"
        ).fetchone()[0]
        changes_before = db.total_changes
    client.get("/system/inventory/configured/services", headers=headers)
    with store._connect() as db:
        assert (
            db.execute("SELECT secret FROM system_inventory_identity WHERE singleton=1").fetchone()[
                0
            ]
            == identity_before
        )
        assert db.total_changes == changes_before == 0

    def unavailable(*args, **kwargs):
        raise ActionError("targets_unavailable", "Managed target information is unavailable.", 503)

    monkeypatch.setattr(system_targets, "read", unavailable)
    failed = client.get("/system/inventory/configured/services", headers=headers)
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "targets_unavailable"
    assert "results" not in failed.text
    store.close()


def test_owner_inventory_allowlist_and_malformed_receipts_fail_closed():
    request_id = "inventory_request_01"
    for path in (
        f"/system/inventory/{request_id}",
        "/system/inventory/configured/services",
        "/system/inventory/configured/containers",
    ):
        assert owner_allowed("GET", path)
        assert not runtime_allowed("GET", path)
    for path in ("/system/inventory", f"/system/inventory/{request_id}/resume"):
        assert owner_allowed("POST", path)
        assert not runtime_allowed("POST", path)
    for method, path in (
        ("GET", "/system/inventory/short"),
        ("GET", "/system/inventory/configured/processes"),
        ("POST", f"/system/inventory/{request_id}/refresh"),
        ("DELETE", f"/system/inventory/{request_id}"),
        ("GET", "/system/targets"),
        ("POST", "/system/actions"),
    ):
        assert not owner_allowed(method, path)

    value = observed_rows()
    value["processes"]["results"][0]["command"] = "secret --token value"
    with pytest.raises(ValueError):
        inventory._projection(value, 10)

    partial = observed()
    partial["source"]["containerScope"] = "unavailable"
    partial["containers"]["errors"] = ["container_telemetry_not_configured"]
    partial["ports"].update(status="partial", errors=["container_bindings_not_configured"])
    projected, _age = inventory._projection(partial, 10)
    assert projected["source"]["containerScope"] == "unavailable"
