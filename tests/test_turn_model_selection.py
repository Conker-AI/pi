"""Per-request model locks survive settings edits and never silently fallback."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_model_roles import Adapter, config

from pi import agents, api, context_retrieval, model_roles, session_settings, submissions, tasks
from pi import turn_queue as q
from pi.loop import Loop, TurnFailed
from pi.routing import Router
from pi.store import Store


def setup(store, second_fail=False):
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(config())
        ),
    )
    first, second = Adapter(), Adapter(fail=second_fail)
    loop = Loop(store, Router(providers={"one": first, "two": second}))
    return loop, first, second


def test_turn_override_is_frozen_and_replay_cannot_change_it(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, first, second = setup(store)
        sid = store.create_session()
        original = context_retrieval.resolve

        def change_after_reservation(*args, **kwargs):
            value = config()
            value["models"][1]["route"] = "changed-b"
            model_roles.save(
                store,
                model_roles.Update(
                    expected_revision=1,
                    configuration=model_roles.Configuration.model_validate(value),
                ),
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(context_retrieval, "resolve", change_after_reservation)
        result = loop.run_turn(sid, "hello", request_id="model_request_001", model_id="b")
        assert first.calls == [] and second.calls == [("actual-b", 1.0)]
        assert session_settings.execution(store, sid, result["turn_id"])["answerModelId"] == "b"
        assert loop.run_turn(sid, "hello", request_id="model_request_001", model_id="b")["replayed"]
        with pytest.raises(submissions.SubmissionError, match="already used"):
            loop.run_turn(sid, "hello", request_id="model_request_001", model_id="a")
        assert len(second.calls) == 1


def test_invalid_model_rolls_back_and_unavailable_model_does_not_fallback(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, first, second = setup(store, second_fail=True)
        sid = store.create_session()
        with pytest.raises(agents.AgentError, match="eligible"):
            loop.run_turn(sid, "hello", request_id="model_invalid_001", model_id="missing")
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM turn_submissions").fetchone()[0] == 0
        with pytest.raises(TurnFailed):
            loop.run_turn(sid, "hello", request_id="model_unavailable_001", model_id="b")
        assert first.calls == [] and len(second.calls) == 1


def test_queue_preserves_model_and_review_can_choose_another(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, first, second = setup(store)
        sid = store.create_session()
        entry = q.enqueue(
            store, sid, q.Enqueue(request_id="model_queue_0001", text="hello", model_id="b")
        )
        assert q.run_next(store, loop, sid)["entries"] == []
        assert second.calls == [("actual-b", 1.0)] and first.calls == []
        entry = q.enqueue(
            store, sid, q.Enqueue(request_id="model_queue_0002", text="next", model_id="b")
        )
        reviewed = q.change(
            store, sid, entry["id"], q.Review(expected_revision=1, model_id="a"), "review"
        )
        assert reviewed["revision"] == 2
        assert q.run_next(store, loop, sid)["entries"] == []
        assert first.calls == [("actual-a", 1.0)]


def test_queue_admission_refuses_substituted_model(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, first, second = setup(store)
        sid = store.create_session()
        entry = q.enqueue(
            store, sid, q.Enqueue(request_id="model_queue_0001", text="hello", model_id="b")
        )
        with pytest.raises(tasks.TaskError, match="match"):
            loop.run_turn(
                sid,
                "hello",
                request_id=q.submission_identity(entry["id"], 1),
                model_id="a",
                queued_entry=(entry["id"], 1),
            )
        assert first.calls == second.calls == []


def test_http_turn_honors_explicit_model(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, first, second = setup(store)
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "loop", loop, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        sid = store.create_session()
        response = TestClient(api.app).post(
            f"/sessions/{sid}/turns",
            headers={"X-Pi-Key": "synthetic-owner"},
            json={"text": "hello", "request_id": "model_http_000001", "model_id": "b"},
        )
        assert response.status_code == 200, response.text
        assert first.calls == [] and second.calls == [("actual-b", 1.0)]
