from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from threading import Event

import pytest

from pi import filesystem_reads as inventory
from pi.store import Store
from pi.toolgate import ApprovalRequired, ToolPending, ToolResult


def make_store(path):
    store = Store(path)
    with store._connect() as db:
        db.executescript(inventory.SCHEMA)
    return store


def observed():
    return {
        "mode": "observed",
        "rootId": "workspace",
        "path": "",
        "sampledAt": datetime.now(UTC).isoformat(),
        "truncated": False,
        "entries": [{"name": "docs", "path": "docs", "kind": "directory"}],
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
    body = inventory.Read(root_id="workspace", request_id="inventory_request_01", limit=12)
    with closing(make_store(path)) as store:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: inventory.request(store, gate, body), range(2)))
        assert len(gate.invocations) == 1
        assert gate.invocations[0][:2] == (
            inventory.TOOL,
            {"root_id": "workspace", "path": "", "limit": 12},
        )
        with pytest.raises(inventory.FileReadError, match="bound"):
            inventory.request(store, gate, body.model_copy(update={"limit": 13}))
    with closing(make_store(path)) as store:
        result = inventory.inspect(store, gate, body.request_id)
        assert result["state"] == "complete" and result["listing"]["entries"][0]["name"] == "docs"
        assert result["currentAgeSeconds"] >= 0
        assert len(gate.invocations) == len(gate.checks) == 1
        with store._connect() as db:
            record = str(dict(db.execute("SELECT * FROM filesystem_reads").fetchone()))
            assert "sampledAt" not in record  # Host observations remain in ToolGate's receipt.


def test_approval_is_not_recreated_and_resumes_exact_saved_request(tmp_path):
    with closing(make_store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ApprovalRequired(
            "approval-one",
            None,
            "Review",
            inventory.TOOL,
            {"root_id": "workspace", "path": "", "limit": 200},
        )
        body = inventory.Read(root_id="workspace", request_id="inventory_approval_01")
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
    with closing(make_store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolPending("outcome_unknown", "Timeout", "action")
        body = inventory.Read(root_id="workspace", request_id="inventory_unknown_01")
        assert inventory.request(store, gate, body)["state"] == "unknown"
        gate.result = ToolResult(True, observed(), inventory.TOOL)
        assert inventory.inspect(store, gate, body.request_id)["state"] == "complete"
        gate.result = ToolPending("outcome_unknown", "Offline", "action")
        result = inventory.inspect(store, gate, body.request_id)
        assert result["state"] == "complete" and result["receiptStatus"] == "unavailable"
        assert result["listing"] is None and len(gate.invocations) == 1
        with store._connect() as db:
            db.execute("UPDATE filesystem_reads SET state='dispatching'")
        assert inventory.recover(store) == 1
        assert inventory.inspect(store, gate, body.request_id)["state"] == "unknown"
        assert len(gate.invocations) == 1


@pytest.mark.parametrize(
    "path", ["/etc", "..", "x/../z", "x//y", "x/", "x\\y", "\x00", "\ud800", "a" * 256]
)
def test_bad_paths(path):
    with pytest.raises(ValueError):
        inventory.Read(request_id="invalid_path_request", root_id="workspace", path=path)


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "configured"},
        {"rootId": "different"},
        {"path": "other"},
        {"truncated": 1},
        {"sampledAt": "2026-09-20T00:00:00"},
        {"entries": [{"name": "..", "path": "..", "kind": "directory"}]},
        {"entries": [{"name": "a", "path": "else/a", "kind": "file"}]},
        {"entries": [{"name": "a", "path": "a", "kind": "unknown"}]},
        {"entries": [{"name": "a", "path": "a", "kind": "file"}] * 2},
        {"entries": None},
    ],
)
def test_malformed_listing_not_exposed(tmp_path, change):
    with closing(make_store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolResult(True, {**observed(), **change}, inventory.TOOL)
        result = inventory.request(
            store, gate, inventory.Read(request_id="invalid_receipt_request", root_id="workspace")
        )
        assert result["state"] == "failed" and result["listing"] is None
        assert result["errorCode"] == "invalid_listing"


def test_limit_payload_binding_and_unconfigured(tmp_path):
    with closing(make_store(tmp_path / "pi.db")) as store:
        gate = Gate()
        body = inventory.Read(request_id="binding_request_01", root_id="workspace", limit=1)
        with pytest.raises(inventory.FileReadError) as error:
            inventory.request(store, None, body)
        assert error.value.status == 503
        assert inventory.request(store, gate, body)["state"] == "complete"
        for update in ({"root_id": "other"}, {"path": "docs"}):
            with pytest.raises(inventory.FileReadError):
                inventory.request(store, gate, body.model_copy(update=update))
        gate.result = ToolResult(
            True,
            {
                **observed(),
                "entries": [
                    {"name": "a", "path": "a", "kind": "file"},
                    {"name": "b", "path": "b", "kind": "symlink"},
                ],
            },
            inventory.TOOL,
        )
        result = inventory.request(
            store, gate, body.model_copy(update={"request_id": "limit_request_001"})
        )
        assert result["state"] == "failed"


def test_projection_and_wrong_approval(tmp_path):
    with closing(make_store(tmp_path / "pi.db")) as store:
        gate = Gate()
        value = observed()
        value["content"] = "hidden"
        value["entries"][0]["content"] = "hidden"
        gate.result = ToolResult(True, value, inventory.TOOL)
        body = inventory.Read(request_id="projection_request_01", root_id="workspace")
        result = inventory.request(store, gate, body)
        assert "content" not in str(result["listing"])
        gate.result = ApprovalRequired("wrong", None, "Review", inventory.TOOL, {"limit": 200})
        result = inventory.request(
            store, gate, body.model_copy(update={"request_id": "wrong_approval_001"})
        )
        assert result["state"] == "failed" and result["approval"] is None
        assert result["errorCode"] == "invalid_approval"


def test_delayed_unknown_poll_cannot_erase_completed_receipt(tmp_path):
    with closing(make_store(tmp_path / "pi.db")) as store:
        gate = Gate()
        gate.result = ToolPending("unknown", "Unavailable", "action")
        body = inventory.Read(request_id="delayed_poll_request", root_id="workspace")
        inventory.request(store, gate, body)
        entered, release = Event(), Event()

        class DelayedGate:
            def check_action(self, action_id, tool):
                entered.set()
                assert release.wait(5)
                return ToolPending("unknown", "Unavailable", action_id)

        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(inventory.inspect, store, DelayedGate(), body.request_id)
            assert entered.wait(5)
            gate.result = ToolResult(True, observed(), inventory.TOOL)
            assert inventory.inspect(store, gate, body.request_id)["state"] == "complete"
            release.set()
            result = pending.result()
        assert result["state"] == "complete"
        assert result["listing"] is None and result["receiptStatus"] == "unavailable"
