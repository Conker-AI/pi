"""Stop races use real turn persistence and preserve in-flight action outcomes."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_loop import Recorder, loop_with
from test_tool_turns import CALL, FakeGate, build

from pi import api, submissions, tasks, turn_control
from pi.loop import ActedWithoutReply, TurnFailed
from pi.providers import ProviderUnavailable
from pi.store import Store


def test_stop_during_provider_discards_answer_and_replay_does_not_restart(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()

        class Stop(Recorder):
            def complete(self, messages, *, model):
                receipt = submissions.get(store, "cancel_request_001")
                first = turn_control.cancel(store, receipt["turn_id"])
                assert turn_control.cancel(store, receipt["turn_id"]) == first
                return super().complete(messages, model=model)

        provider = Stop()
        loop = loop_with(store, provider)
        with pytest.raises(TurnFailed, match="stopped"):
            loop.run_turn(session, "hello", request_id="cancel_request_001")
        receipt = submissions.get(store, "cancel_request_001")
        assert receipt["status"] == "cancelled"
        assert [m["role"] for m in store.messages(session)] == ["user"]
    with closing(Store(path)) as store:
        provider = Recorder()
        result = loop_with(store, provider).run_turn(
            session, "hello", request_id="cancel_request_001"
        )
        assert result["status"] == "cancelled" and provider.calls == []


def test_stop_during_effect_keeps_receipt_without_narration_or_replay(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()

        class StopGate(FakeGate):
            def invoke(self, *args, **kwargs):
                receipt = submissions.get(store, "cancel_effect_001")
                turn_control.cancel(store, receipt["turn_id"])
                return super().invoke(*args, **kwargs)

        gate = StopGate()
        loop, provider = build(store, [CALL, "must not be generated"], gate)
        with pytest.raises(ActedWithoutReply):
            loop.run_turn(session, "do it", request_id="cancel_effect_001")
        receipt = submissions.get(store, "cancel_effect_001")
        assert receipt["status"] == "acted_no_reply" and receipt["acted"]
        assert len(gate.invocations) == 1 and len(provider.sent) == 1
        assert [m["role"] for m in store.messages(session)] == ["user", "assistant", "tool"]
        assert receipt["final_message_id"] is None


def test_stop_wins_atomic_final_commit_but_cannot_change_finished_turn(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        turn = store.start_turn(session)
        turn_control.cancel(store, turn)
        with pytest.raises(ProviderUnavailable, match="stopped"):
            store.complete_turn(turn, "late result")
        assert store.messages(session) == []
        store.finish_turn(turn, "failed")
        assert store.get_turn(turn)["status"] == "cancelled"
        other = store.start_turn(session)
        store.complete_turn(other, "finished")
        with pytest.raises(tasks.TaskError) as exc:
            turn_control.cancel(store, other)
        assert exc.value.status == 409
        assert store.get_turn(other)["status"] == "complete"


def test_cancel_route_requires_owner_credential(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        turn = store.start_turn(store.create_session())
        client = TestClient(api.app)
        assert client.post(f"/turns/{turn}/cancel").status_code == 401
        response = client.post(f"/turns/{turn}/cancel", headers={"X-Pi-Key": "synthetic-owner"})
        assert response.status_code == 200 and response.json()["cancel_requested"]


def test_stop_does_not_prevent_unknown_effect_reconciliation(tmp_path):
    from pi.toolgate import ToolGateUnavailable, ToolResult

    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()

        class LostReceipt(FakeGate):
            def invoke(self, *args, **kwargs):
                turn_control.cancel(store, submissions.get(store, "cancel_unknown_001")["turn_id"])
                super().invoke(*args, **kwargs)
                raise ToolGateUnavailable("receipt lost")

            def check_action(self, action_id, tool_id):
                return ToolResult(ok=True, result={"done": True}, tool_id=tool_id)

        gate = LostReceipt()
        loop, provider = build(store, [CALL, "not generated"], gate)
        result = loop.run_turn(session, "do it", request_id="cancel_unknown_001")
        assert result["status"] == "outcome_unknown"
        with pytest.raises(ActedWithoutReply):
            loop.resume_turn(result["turn_id"])
        receipt = submissions.get(store, "cancel_unknown_001")
        assert receipt["cancel_requested"] and receipt["acted"]
        assert receipt["status"] == "acted_no_reply"
        assert len(gate.invocations) == len(provider.sent) == 1
