from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import system_actions as actions
from pi import system_actions_api
from pi.store import Store
from pi.toolgate import ApprovalRequired, ToolGateClient, ToolPending, ToolRefused, ToolResult

ID = "a" * 64
BODY = actions.Request(request_id="container_action_01", container_id=ID, action="restart")


def observed():
    state = {
        "status": "running",
        "running": True,
        "paused": False,
        "restarting": False,
        "dead": False,
        "Env": "secret",
    }
    return {
        "containerId": ID,
        "action": "restart",
        "outcome": "observed",
        "dispatched": True,
        "before": state,
        "after": state,
        "Config": "secret",
    }


class Gate:
    def __init__(self):
        self.invocations, self.checks = [], []
        self.result = ApprovalRequired(
            "approval1", None, "Review", actions.TOOL, {"container_id": ID, "action": "restart"}
        )

    def invoke(self, *args, **kwargs):
        self.invocations.append((args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def check_action(self, *args):
        self.checks.append(args)
        return self.result


def test_concurrent_requests_and_resumes_dispatch_once(tmp_path):
    gate = Gate()
    with closing(Store(tmp_path / "pi.db")) as store:
        with ThreadPoolExecutor(2) as pool:
            rows = list(pool.map(lambda _: actions.request(store, gate, BODY), range(2)))
        assert len(gate.invocations) == 1
        assert any(row["state"] == "awaiting_approval" for row in rows)
        with pytest.raises(actions.ActionError, match="another"):
            actions.request(store, gate, BODY.model_copy(update={"action": "stop"}))
        gate.result = ToolResult(True, observed(), actions.TOOL)
        with ThreadPoolExecutor(2) as pool:
            rows = list(pool.map(lambda _: actions.resume(store, gate, BODY.request_id), range(2)))
        assert len(gate.invocations) == 2
        assert gate.invocations[-1][1]["approval_request_id"] == "approval1"
        completed = next(row for row in rows if row["observation"])
        assert completed["state"] == "complete" and "secret" not in str(completed)
        assert len(actions.history(store)) == 1


def test_restart_unknown_and_read_receipt_never_reexecutes(tmp_path):
    path, gate = tmp_path / "pi.db", Gate()
    gate.result = RuntimeError("sensitive lost response")
    with closing(Store(path)) as store:
        assert actions.request(store, gate, BODY)["state"] == "unknown"
        with store._connect() as db:
            db.execute("UPDATE system_actions SET state='dispatching'")
    with closing(Store(path)) as store:
        assert actions.recover(store) == 1
        gate.result = ToolResult(True, observed(), actions.TOOL)
        result = actions.inspect(store, gate, BODY.request_id)
        assert result["state"] == "complete" and len(gate.invocations) == 1
        gate.result = ToolPending("outcome_unknown", "offline", "identity")
        result = actions.inspect(store, gate, BODY.request_id)
        assert result["state"] == "complete" and result["receiptStatus"] == "unavailable"
        actions.resume(store, gate, BODY.request_id)
        assert len(gate.invocations) == 1


@pytest.mark.parametrize(
    "change",
    [{"containerId": "b" * 64}, {"action": "stop"}, {"before": None}, {"dispatched": "true"}],
)
def test_invalid_success_stays_unknown_not_false_failure(tmp_path, change):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolResult(True, {**observed(), **change}, actions.TOOL)
        result = actions.request(store, gate, BODY)
        assert result["state"] == "unknown" and result["errorCode"] == "invalid_receipt"
        assert result["observation"] is None


def test_wrong_approval_never_becomes_resumable(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ApprovalRequired("bad", None, "secret", actions.TOOL, {"action": "stop"})
        result = actions.request(store, gate, BODY)
        assert result["state"] == "unknown" and result["approval"] is None
        actions.resume(store, gate, BODY.request_id)
        assert len(gate.invocations) == 1


def test_unconfigured_and_known_refusal(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        with pytest.raises(actions.ActionError, match="not configured"):
            actions.request(store, None, BODY)
        assert actions.history(store) == []
        gate = Gate()
        gate.result = ToolRefused("DENIED", "raw secret")
        result = actions.request(store, gate, BODY)
        assert result["state"] == "failed" and "raw secret" not in str(result)


def test_routes_require_owner_and_history_does_not_call_gate(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()

        def authorize(x_owner: str | None = Header(None)):
            if x_owner != "owner":
                raise HTTPException(401)

        app = FastAPI()
        app.include_router(system_actions_api.router(lambda: store, lambda: gate, authorize))
        with TestClient(app) as client:
            assert client.post("/system/actions", json=BODY.model_dump()).status_code == 401
            assert client.get("/system/actions").status_code == 401
            assert not gate.invocations
            headers = {"X-Owner": "owner"}
            assert (
                client.post("/system/actions", json=BODY.model_dump(), headers=headers).status_code
                == 200
            )
            assert len(client.get("/system/actions", headers=headers).json()["results"]) == 1
            assert client.get("/system/actions?limit=101", headers=headers).status_code == 422
            assert len(gate.invocations) == 1 and not gate.checks


def test_real_gate_client_keeps_exact_action_and_saved_approval(tmp_path, monkeypatch):
    sent = []

    def post(url, **kwargs):
        sent.append((url, kwargs["json"]))
        assert url == "http://gate.test/v2/tools/system.container-control/invoke"
        assert kwargs["json"]["args"] == {"container_id": ID, "action": "restart"}
        if len(sent) == 1:
            return httpx.Response(
                200,
                json={
                    "code": "CONFIRMATION_REQUIRED",
                    "request_id": "saved-approval",
                    "expires_at": None,
                },
            )
        assert kwargs["json"]["approval_request_id"] == "saved-approval"
        return httpx.Response(
            200,
            json={
                "code": "OK",
                "status": "completed",
                "action_id": kwargs["json"]["action_id"],
                "result": {"ok": True, "result": observed()},
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    gate = ToolGateClient("http://gate.test", "synthetic-key")
    with closing(Store(tmp_path / "pi.db")) as store:
        assert actions.request(store, gate, BODY)["state"] == "awaiting_approval"
        assert actions.resume(store, gate, BODY.request_id)["state"] == "complete"
        actions.request(store, gate, BODY)
        actions.resume(store, gate, BODY.request_id)
        assert len(sent) == 2
        assert sent[0][1]["action_id"] == sent[1][1]["action_id"]
