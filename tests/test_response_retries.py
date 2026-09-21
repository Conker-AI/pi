from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest
from test_attachment_turns import upload
from test_model_roles import Adapter, config

from pi import (
    context_controls,
    forgetting,
    model_roles,
    tasks,
    turn_control,
    turn_queue,
)
from pi import (
    response_retries as retries,
)
from pi.loop import Loop
from pi.providers import ProviderUnavailable
from pi.routing import Router
from pi.store import Store


class Recording(Adapter):
    def __init__(self, response="Alternative", callback=None):
        super().__init__(response)
        self.messages = []
        self.callback = callback

    def complete_bounded(self, messages, *, model, timeout):
        self.messages.append(list(messages))
        if self.callback:
            self.callback()
        return super().complete_bounded(messages, model=model, timeout=timeout)


def setup(store, second=None):
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(config())
        ),
    )
    first, second = Recording("Original"), second or Recording()
    return Loop(store, Router(providers={"one": first, "two": second})), first, second


def body(identity="retry_request_0001"):
    return retries.Retry(request_id=identity, model_id="b")


def test_retry_exact_boundary_selected_model_no_duplicate_input_or_later_context(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _first, second = setup(store)
        sid = store.create_session()
        original = loop.run_turn(sid, "Original request", request_id="original_request_001")
        store.append_message(sid, "user", "Later question")
        result = retries.run(loop, sid, original["message"]["id"], body())
        assert result["status"] == "complete"
        assert result["input_message_id"] == original["submission"]["input_message_id"]
        from pi import turn_context

        assert turn_context.replay(store, result["turn_id"])["messages"] == second.messages[0]
        assert [m.content for m in second.messages[0]] == [retries.NARRATION, "Original request"]
        assert second.calls == [("actual-b", 1.0)]
        assert [r["content"] for r in context_controls.history(store, sid)] == [
            "Original request",
            "Alternative",
            "Later question",
        ]
        assert len([m for m in store.messages(sid) if m["role"] == "user"]) == 2
        assert turn_queue.read(store, sid)["paused"]
        assert retries.run(loop, sid, original["message"]["id"], body())["replayed"]
        assert len(second.calls) == 1
        with pytest.raises(tasks.TaskError):
            retries.run(
                loop, sid, original["message"]["id"], body().model_copy(update={"model_id": "a"})
            )


def test_retry_of_retry_retains_original_input_and_citation(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        attachment = upload(store, sid)
        second = Recording(f"From source [[{attachment}:p0]]")
        loop, _, _ = setup(store, second)
        original = loop.run_turn(
            sid, "Read file", request_id="retry_source_0001", attachment_ids=[attachment]
        )
        first = retries.run(loop, sid, original["message"]["id"], body())
        next_ = retries.run(loop, sid, first["message"]["id"], body("retry_request_0002"))
        assert next_["message"]["citations"][0]["id"] == f"{attachment}:p0"
        assert sum(m.content == retries.NARRATION for m in second.messages[-1]) == 1
        assert [m["content"] for m in store.messages(sid) if m["role"] == "user"] == ["Read file"]
        assert first["message"]["response_family"]["root_message_id"] == original["message"]["id"]
        assert next_["message"]["response_family"]["root_message_id"] == original["message"]["id"]


def test_concurrent_duplicate_and_stop_do_not_repeat_provider(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        entered, release = Event(), Event()

        def wait():
            entered.set()
            assert release.wait(5)

        second = Recording(callback=wait)
        loop, _, _ = setup(store, second)
        sid = store.create_session()
        original = loop.run_turn(sid, "Question")["message"]["id"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(retries.run, loop, sid, original, body())
            assert entered.wait(5)
            duplicate = retries.run(loop, sid, original, body())
            assert duplicate["status"] == "running" and duplicate["replayed"]
            turn_control.cancel(store, duplicate["turn_id"])
            release.set()
            with pytest.raises(ProviderUnavailable):
                future.result()
        result = retries.run(loop, sid, original, body())
        assert result["status"] == "cancelled" and result["message"] is None
        assert len(second.calls) == 1
        assert context_controls.history(store, sid)[-1]["id"] == original


def test_owner_api_restart_failed_receipt_and_forgetting(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pi import api

    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        second = Recording()
        second.fail = True
        loop, _, _ = setup(store, second)
        sid = store.create_session()
        original = loop.run_turn(sid, "Request")["message"]["id"]
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "loop", loop, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        client = TestClient(api.app)
        url = f"/sessions/{sid}/messages/{original}/retry"
        assert client.post(url, json=body().model_dump()).status_code == 401
        result = client.post(url, json=body().model_dump(), headers={"X-Pi-Key": "synthetic-owner"})
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "failed"
    with closing(Store(path)) as store:
        loop = Loop(store, Router())
        assert retries.run(loop, sid, original, body())["status"] == "failed"
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    with closing(Store(path)) as store, pytest.raises(tasks.TaskError):
        retries.receipt(store, sid, body().request_id)


def test_tool_answer_retry_never_dispatches_again(tmp_path):
    from test_tool_turns import CALL, FakeGate, build

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        gate = FakeGate()
        original_loop, _ = build(store, [CALL, "Action result"], gate)
        original = original_loop.run_turn(sid, "Do it")
        loop, _, second = setup(store, Recording(CALL))
        loop.toolgate = gate
        result = retries.run(loop, sid, original["message"]["id"], body())
        assert result["status"] == "complete"
        assert len(gate.invocations) == 1
        assert any(m.role == "tool" for m in second.messages[0])
        assert store.get_turn(result["turn_id"])["acted"] == 0


def test_restart_keeps_interrupted_attempt_without_recalling_provider(tmp_path):
    class ProcessDied(BaseException):
        pass

    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:

        def die():
            raise ProcessDied()

        loop, _, _ = setup(store, Recording(callback=die))
        sid = store.create_session()
        original = loop.run_turn(sid, "Request")["message"]["id"]
        with pytest.raises(ProcessDied):
            retries.run(loop, sid, original, body())
    with closing(Store(path)) as store:
        store.mark_interrupted_turns()
        loop = Loop(store, Router())
        result = retries.run(loop, sid, original, body())
        assert result["status"] == "interrupted" and result["replayed"]
        assert result["message"] is None
        assert turn_queue.read(store, sid)["paused"]


def test_new_policy_does_not_change_retry_and_pinned_original_is_not_replaced(tmp_path):
    from test_context_controls import policy

    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _, second = setup(store)
        sid = store.create_session()
        original = loop.run_turn(sid, "Request")["message"]["id"]
        changed = policy({original: "keep-exact"})
        changed.sessionInstructions = "New instructions must not enter old context"
        context_controls.save(
            store, sid, context_controls.Update(expected_revision=0, policy=changed)
        )
        result = retries.run(loop, sid, original, body())
        assert all("New instructions" not in m.content for m in second.messages[0])
        assert result["message"]["response_family"]["selected_message_id"] == original
        assert context_controls.load(store, sid, result["turn_id"])["policy"] is None


def test_invalid_model_and_stricter_privacy_do_not_create_attempt(tmp_path):
    from pi import agents, session_settings

    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _, second = setup(store)
        sid = store.create_session()
        original = loop.run_turn(sid, "Request")["message"]["id"]
        with pytest.raises(agents.AgentError):
            retries.run(loop, sid, original, body().model_copy(update={"model_id": "missing"}))
        settings = session_settings.Settings.model_validate(session_settings.DEFAULT)
        settings.privacy.memoryDisabled = True
        session_settings.save(
            store, sid, session_settings.Update(expected_revision=0, settings=settings)
        )
        with pytest.raises(context_controls.ContextError):
            retries.run(loop, sid, original, body())
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM response_retries").fetchone()[0] == 0
        assert second.calls == []
