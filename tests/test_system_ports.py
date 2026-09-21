import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import system_actions as actions
from pi import system_actions_api
from pi.store import Store
from pi.toolgate import ApprovalRequired, ToolPending, ToolResult

CID, RID, NEW = "a" * 64, "b" * 48, "c" * 64
BODY = actions.PortRequest(request_id="port_replacement_01", container_id=CID, review_id=RID)


def observation():
    return {
        "containerId": CID,
        "replacementId": NEW,
        "snapshotImage": "sha256:" + "d" * 64,
        "originalRetained": True,
        "outcome": "observed",
        "dispatched": True,
        "bindings": [
            {
                "hostAddress": "127.0.0.1",
                "hostPort": 8080,
                "containerPort": 80,
                "protocol": "tcp",
                "Env": "private-value",
            }
        ],
        "Env": "private-value",
    }


class Gate:
    def __init__(self):
        self.calls = []
        self.result = ApprovalRequired(
            "review-approval",
            None,
            "Review",
            actions.PORT_TOOL,
            {"container_id": CID, "review_id": RID},
        )

    def invoke(self, tool, args, **options):
        self.calls.append((tool, args, options))
        return self.result

    def check_action(self, identity, tool):
        assert identity == "pi_system_" + BODY.request_id and tool == actions.PORT_TOOL
        return self.result


def test_concurrent_port_requests_and_saved_approval_resume(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: actions.request(store, gate, BODY), range(2)))
        assert len(gate.calls) == 1
        assert gate.calls[0][0:2] == (actions.PORT_TOOL, {"container_id": CID, "review_id": RID})
        gate.result = ToolResult(True, observation(), actions.PORT_TOOL)
        result = actions.resume(store, gate, BODY.request_id)
        assert result["state"] == "complete" and result["reviewId"] == RID
        assert result["observation"]["replacementId"] == NEW
        assert "private-value" not in json.dumps(result)
        assert gate.calls[-1][2]["approval_request_id"] == "review-approval"
        assert actions.history(store)[0]["action"] == "ports"
        assert actions.inspect(store, gate, BODY.request_id)["state"] == "complete"
        assert len(gate.calls) == 2
        with pytest.raises(actions.ActionError):
            actions.request(store, gate, BODY.model_copy(update={"review_id": "e" * 48}))


@pytest.mark.parametrize(
    "change",
    [
        {"containerId": NEW},
        {"replacementId": CID},
        {"replacementId": "short"},
        {"snapshotImage": "mutable:tag"},
        {"originalRetained": False},
        {"bindings": None},
        {
            "bindings": [
                {"hostAddress": "localhost", "hostPort": 80, "containerPort": 80, "protocol": "tcp"}
            ]
        },
    ],
)
def test_bad_success_never_becomes_false_completion(tmp_path, change):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolResult(True, {**observation(), **change}, actions.PORT_TOOL)
        result = actions.request(store, gate, BODY)
        assert result["state"] == "unknown" and result["errorCode"] == "invalid_receipt"


def test_unchanged_and_unknown_receipts(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolPending("outcome_unknown", "not confirmed", "action")
        assert actions.request(store, gate, BODY)["state"] == "unknown"
        gate.result = ToolResult(
            True,
            {
                "containerId": CID,
                "replacementId": None,
                "outcome": "unchanged",
                "dispatched": False,
            },
            actions.PORT_TOOL,
        )
        assert actions.inspect(store, gate, BODY.request_id)["state"] == "complete"
        assert len(gate.calls) == 1


def test_owner_port_endpoint_preserves_shared_routes(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate, app = Gate(), FastAPI()

        def owner(x_owner: str | None = Header(None)):
            if x_owner != "owner":
                raise HTTPException(401)

        app.include_router(system_actions_api.router(lambda: store, lambda: gate, owner))
        client = TestClient(app)
        assert client.post("/system/actions/ports", json=BODY.model_dump()).status_code == 401
        result = client.post(
            "/system/actions/ports", json=BODY.model_dump(), headers={"x-owner": "owner"}
        )
        assert result.status_code == 200 and result.json()["state"] == "awaiting_approval"
        assert len(gate.calls) == 1
