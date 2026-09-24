"""Owner recovery narrates completed effects without re-dispatching them."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_tool_turns import CALL, FakeGate, build

from pi import api, submissions, tasks, turn_control
from pi.loop import ActedWithoutReply
from pi.providers import ProviderUnavailable
from pi.store import Store


def stopped(store):
    sid = store.create_session()

    class StopGate(FakeGate):
        def invoke(self, *args, **kwargs):
            turn_control.cancel_submission(store, "reply_origin_001")
            return super().invoke(*args, **kwargs)

    gate = StopGate()
    loop, provider = build(store, [CALL, "The action completed."], gate)
    with pytest.raises(ActedWithoutReply):
        loop.run_turn(sid, "Act once", request_id="reply_origin_001")
    return loop, provider, gate, submissions.get(store, "reply_origin_001")["turn_id"]


def test_reply_after_stop_is_once_durable_and_has_no_tools(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        loop, provider, gate, turn = stopped(store)
        result = loop.recover_reply(turn, "reply_recovery_001")
        assert result["status"] == "complete" and result["acted"]
        assert result["message"]["content"] == "The action completed."
        assert len(gate.invocations) == 1 and len(provider.sent) == 2
        assert any(
            message.role == "system"
            and "recorded tool result" in message.content
            and "Do not repeat a tool call" in message.content
            for message in provider.sent[-1]
        )
        assert loop.recover_reply(turn, "reply_recovery_001")["replayed"]
        assert len(provider.sent) == 2
    with closing(Store(path)) as store:
        loop, provider = build(store, ["must not run"], FakeGate())
        assert loop.recover_reply(turn, "reply_recovery_001")["status"] == "complete"
        assert provider.sent == []
        with pytest.raises(tasks.TaskError):
            loop.recover_reply(store.start_turn(store.create_session()), "reply_recovery_001")


def test_new_stop_invalidates_reply_consent_and_retry_never_restarts(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider, gate, turn = stopped(store)
        original = provider.complete

        def stop_again(messages, *, model):
            turn_control.cancel(store, turn)
            return original(messages, model=model)

        provider.complete = stop_again
        with pytest.raises(ActedWithoutReply):
            loop.recover_reply(turn, "reply_recovery_001")
        assert store.get_turn(turn)["status"] == "acted_no_reply"
        assert loop.recover_reply(turn, "reply_recovery_001")["replayed"]
        assert len(provider.sent) == 2 and len(gate.invocations) == 1
        with pytest.raises(ActedWithoutReply):
            loop.resume_turn(turn)
        assert len(provider.sent) == 2
        provider.complete = original
        provider.replies.append("Now reporting the saved result.")
        assert loop.recover_reply(turn, "reply_recovery_002")["status"] == "complete"
        assert len(gate.invocations) == 1


def test_claim_is_single_and_refuses_unresolved_effects(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        _, _, _, turn = stopped(store)
        with store._connect() as db:
            db.execute("UPDATE tool_actions SET state='outcome_unknown' WHERE turn_id=?", (turn,))
        with pytest.raises(tasks.TaskError, match="Reconcile"):
            turn_control.claim_reply(store, turn, "reply_claim_001")
        with store._connect() as db:
            db.execute("UPDATE tool_actions SET state='completed' WHERE turn_id=?", (turn,))
        assert turn_control.claim_reply(store, turn, "reply_claim_001")[1]
        assert not turn_control.claim_reply(store, turn, "reply_claim_001")[1]
        with pytest.raises(tasks.TaskError):
            turn_control.claim_reply(store, turn, "reply_claim_002")
        turn_control.cancel(store, turn)
        with pytest.raises(ProviderUnavailable):
            store.complete_turn(turn, "late", reply_request_id="reply_claim_001")


def test_reply_route_requires_owner_and_returns_saved_result(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _, gate, turn = stopped(store)
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "loop", loop, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        client = TestClient(api.app)
        url = f"/turns/{turn}/reply-only"
        body = {"request_id": "reply_route_001"}
        assert client.post(url, json=body).status_code == 401
        response = client.post(url, json=body, headers={"X-Pi-Key": "synthetic-owner"})
        assert response.status_code == 200 and response.json()["status"] == "complete"
        assert len(gate.invocations) == 1
