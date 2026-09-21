from contextlib import closing

import pytest
from test_loop import Recorder, loop_with
from test_tool_turns import CALL, FakeGate, build

from pi import tasks
from pi import turn_steering as steering
from pi.providers import Completion
from pi.store import Store


def request(text="Use the new instructions", identity="steer_request_0001"):
    return steering.Steer(request_id=identity, text=text)


class SteerOnce(Recorder):
    def __init__(self, store, sid, first="Stale answer"):
        super().__init__()
        self.store, self.sid, self.first = store, sid, first

    def complete(self, messages, *, model):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            self.turn = next(t for t in self.store.turns(self.sid) if t["status"] == "running")[
                "id"
            ]
            self.receipt = steering.submit(self.store, self.turn, request())
            return Completion(text=self.first, model=model, provider=self.name)
        return Completion(text="Revised answer", model=model, provider=self.name)


def test_running_model_is_steered_without_new_turn_or_stale_answer(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        provider = SteerOnce(store, sid)
        result = loop_with(store, provider).run_turn(sid, "First request")
        assert result["message"]["content"] == "Revised answer"
        assert len(store.turns(sid)) == 1
        assert [m["content"] for m in store.messages(sid)] == [
            "First request",
            "Use the new instructions",
            "Revised answer",
        ]
        assert provider.calls[-1][-1].content == "Use the new instructions"
        replay = steering.submit(store, provider.turn, request())
        assert replay["state"] == "applied" and replay["replayed"]
        with pytest.raises(tasks.TaskError):
            steering.submit(store, provider.turn, request("Changed"))


def test_stale_tool_proposal_is_never_dispatched(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        gate = FakeGate()
        provider = SteerOnce(store, sid, CALL)
        loop = loop_with(store, provider, toolgate=gate)
        loop.run_turn(sid, "First request")
        assert gate.invocations == []
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM tool_actions").fetchone()[0] == 0
        assert all(m["content"] != CALL for m in store.messages(sid))


def test_action_already_admitted_refuses_steer_and_keeps_receipt(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()

        class InspectingGate(FakeGate):
            def invoke(self, *args, **kwargs):
                tid = next(t for t in store.turns(sid) if t["status"] == "running")["id"]
                with pytest.raises(tasks.TaskError) as error:
                    steering.submit(store, tid, request())
                assert error.value.detail["code"] == "action_in_flight"
                return super().invoke(*args, **kwargs)

        gate = InspectingGate()
        loop, _ = build(store, [CALL, "Done"], gate)
        result = loop.run_turn(sid, "Use tool")
        assert result["acted"] and len(gate.invocations) == 1
        assert len([m for m in store.messages(sid) if m["role"] == "user"]) == 1


def test_steering_race_at_final_commit_rebuilds_without_publishing_stale_text(
    tmp_path, monkeypatch
):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        original = store.complete_turn
        injected = False

        def commit(tid, text, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                steering.submit(store, tid, request())
            return original(tid, text, **kwargs)

        monkeypatch.setattr(store, "complete_turn", commit)
        provider = Recorder()
        result = loop_with(store, provider).run_turn(sid, "Question")
        assert result["message"]["content"] == "answered"
        assert len(provider.calls) == 2
        assert len([m for m in store.messages(sid) if m["role"] == "assistant"]) == 1


def test_steering_after_action_preserves_acted_state_on_later_failure(tmp_path):
    from test_tool_turns import Scripted

    from pi.loop import ActedWithoutReply
    from pi.providers import ProviderUnavailable

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        gate = FakeGate()

        class Provider(Scripted):
            def complete(self, messages, *, model):
                if len(self.sent) == 1:
                    tid = next(t for t in store.turns(sid) if t["status"] == "running")["id"]
                    steering.submit(store, tid, request())
                return super().complete(messages, model=model)

        provider = Provider([CALL, "Stale after action", ProviderUnavailable("offline")])
        with pytest.raises(ActedWithoutReply):
            loop_with(store, provider, toolgate=gate).run_turn(sid, "Use tool")
        assert store.turns(sid)[0]["status"] == "acted_no_reply"
        assert len(gate.invocations) == 1


def test_discarded_reported_cost_is_preserved_and_unknown_is_not_zero(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()

        class Metered(SteerOnce):
            def complete(self, messages, *, model):
                response = super().complete(messages, model=model)
                return Completion(
                    text=response.text,
                    provider=response.provider,
                    model=model,
                    input_tokens=3,
                    output_tokens=2,
                    cached_tokens=0,
                    cost_usd=0.01,
                )

        provider = Metered(store, sid)
        result = loop_with(store, provider).run_turn(sid, "Question")
        turn = store.get_turn(result["turn_id"])
        assert turn["cost_usd"] == pytest.approx(0.02)
        assert turn["input_tokens"] == 6 and turn["output_tokens"] == 4
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM steering_discarded_answers").fetchone()[0] == 1


def test_owner_route_pending_restart_and_forgetting(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pi import api, forgetting

    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        steering.active(store, tid, True)
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        client = TestClient(api.app)
        url = f"/turns/{tid}/steer"
        assert client.post(url, json=request().model_dump()).status_code == 401
        response = client.post(
            url, json=request().model_dump(), headers={"X-Pi-Key": "synthetic-owner"}
        )
        assert response.status_code == 200 and response.json()["state"] == "pending"
    with closing(Store(path)) as store:
        store.mark_interrupted_turns()
        assert steering.submit(store, tid, request())["state"] == "not_applied"
        with pytest.raises(tasks.TaskError):
            steering.submit(store, tid, request(identity="steer_request_0002"))
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    with closing(Store(path)) as store:
        with pytest.raises(tasks.TaskError):
            steering.submit(store, tid, request())
        with store._connect() as db:
            assert db.execute("SELECT payload_hash FROM turn_steers").fetchone()[0] is None


def test_late_steer_before_action_admission_does_not_leave_stale_proposal(tmp_path, monkeypatch):
    from pi import actions

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        original = actions.prepare
        injected = False

        def prepare(store_, tid, *args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                steering.submit(store_, tid, request())
            return original(store_, tid, *args, **kwargs)

        monkeypatch.setattr(actions, "prepare", prepare)
        gate = FakeGate()
        loop, _ = build(store, [CALL, "Changed direction"], gate)
        result = loop.run_turn(sid, "Use tool")
        assert result["message"]["content"] == "Changed direction"
        assert gate.invocations == []
        assert all(m["content"] != CALL for m in store.messages(sid))


def test_later_context_policy_edit_does_not_rewrite_the_running_turn(tmp_path):
    from test_context_controls import policy

    from pi import context_controls

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()

        class Excluding(SteerOnce):
            def complete(self, messages, *, model):
                result = super().complete(messages, model=model)
                context_controls.save(
                    store,
                    sid,
                    context_controls.Update(
                        expected_revision=0, policy=policy({self.receipt["message_id"]: "exclude"})
                    ),
                )
                return result

        # The turn's frozen policy must win: current owner edits do not silently
        # alter the already submitted turn. Steering remains in its actual context.
        provider = Excluding(store, sid)
        # Only edit once; the original policy remains frozen for the second call.
        original = provider.complete

        def once(messages, *, model):
            if provider.calls:
                return Recorder.complete(provider, messages, model=model)
            return original(messages, model=model)

        provider.complete = once
        loop_with(store, provider).run_turn(sid, "Question")
        assert len(provider.calls) == 2
        assert provider.calls[-1][-1].content == request().text


def test_terminal_inspection_does_not_mark_unapplied_steering_as_applied(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        steering.active(store, tid, True)
        steering.submit(store, tid, request())
        store.finish_turn(tid, "failed")
        loop_with(store, Recorder())._history(sid, turn_id=tid)
        assert steering.submit(store, tid, request())["state"] == "not_applied"
