from contextlib import closing

import pytest
from pydantic import ValidationError

from pi import system_actions as actions
from pi import system_targets
from pi.store import Store
from pi.toolgate import ApprovalRequired, ToolGateClient, ToolResult
from tests.test_system_actions import Gate
from tests.test_system_targets import transport

SERVICE = "user:worker.service"
BODY = actions.ServiceRequest(request_id="managed_service_01", service_id=SERVICE, action="restart")


def observed():
    state = {
        "id": "worker.service",
        "loadState": "loaded",
        "activeState": "active",
        "subState": "running",
        "mainPid": 1234,
        "secret": "discard",
    }
    return {
        "serviceId": SERVICE,
        "action": "restart",
        "dispatched": True,
        "outcome": "observed",
        "before": state,
        "after": state,
    }


def test_service_uses_same_durable_actions_without_container_authority(tmp_path):
    path, gate = tmp_path / "pi.db", Gate()
    gate.result = ApprovalRequired(
        "service-approval",
        None,
        "Review",
        actions.SERVICE_TOOL,
        {"service_id": SERVICE, "action": "restart"},
    )
    with closing(Store(path)) as store:
        assert actions.request(store, gate, BODY)["state"] == "awaiting_approval"
        assert gate.invocations[0][0][0] == actions.SERVICE_TOOL
        changed = BODY.model_copy(update={"service_id": "system:worker.service"})
        with pytest.raises(actions.ActionError):
            actions.request(store, gate, changed)
    with closing(Store(path)) as store:
        gate.result = ToolResult(True, observed(), actions.SERVICE_TOOL)
        result = actions.resume(store, gate, BODY.request_id)
        assert result["state"] == "complete" and result["serviceId"] == SERVICE
        assert "containerId" not in result and "discard" not in str(result)
        assert gate.invocations[-1][1]["approval_request_id"] == "service-approval"
        assert actions.inspect(store, gate, BODY.request_id)["state"] == "complete"
        assert gate.checks[-1][1] == actions.SERVICE_TOOL
        actions.request(store, gate, BODY)
        assert len(gate.invocations) == 2


@pytest.mark.parametrize(
    "service",
    [
        "worker.service",
        "user:../worker.service",
        "user:*.service",
        "system:--bad.service",
        "other:worker.service",
    ],
)
def test_unscoped_or_unsafe_service_names_rejected(service):
    with pytest.raises(ValidationError):
        actions.ServiceRequest(request_id=BODY.request_id, service_id=service, action="stop")


def test_wrong_service_receipt_keeps_uncertainty(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolResult(
            True, {**observed(), "serviceId": "system:worker.service"}, actions.SERVICE_TOOL
        )
        result = actions.request(store, gate, BODY)
        assert result["state"] == "unknown" and result["errorCode"] == "invalid_receipt"


def test_service_discovery_keeps_scope_and_separate_source():
    value = {
        "kind": "configured-targets",
        "status": "configured",
        "services": [SERVICE],
        "actions": ["start", "stop", "restart"],
        "requiresApproval": True,
        "observed": False,
        "source": "toolgate/process-control",
    }
    calls = []
    result = system_targets.read(
        ToolGateClient("http://gate.test", "key"), services=True, transport=transport(value, calls)
    )
    assert result == value and calls[0].url.path == "/v2/agent/system/services"
