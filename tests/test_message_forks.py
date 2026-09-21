"""Branches preserve exact source identity without new memory or action effects."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_loop import Recorder, loop_with

from pi import agents, api, context_controls, session_settings, tasks
from pi import message_forks as forks
from pi.store import Store


def request(**kwargs):
    return forks.Fork(
        request_id="message_fork_0001",
        expected_settings_revision=0,
        expected_context_revision=0,
        **kwargs,
    )


def test_branch_exact_prefix_keeps_original_open_and_does_not_copy_memory(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        first = store.append_message(sid, "user", "First")
        target = store.append_message(sid, "assistant", "Second")
        store.append_message(sid, "user", "Must not inherit")
        with store._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0]
        result = forks.create(store, sid, target["id"], request())
        child = result["session_id"]
        assert store.get_session(sid)["status"] == "open" and store.messages(child) == []
        assert [m["id"] for m in context_controls.history(store, child)] == [
            first["id"],
            target["id"],
        ]
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == count
            assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        provider = Recorder()
        loop_with(store, provider).run_turn(child, "Continue here")
        assert "Must not inherit" not in [m.content for m in provider.calls[0]]
    with closing(Store(path)) as store:
        assert forks.create(store, sid, target["id"], request())["session_id"] == child
        with pytest.raises(tasks.TaskError, match="already used"):
            forks.create(store, sid, first["id"], request())


def test_private_prefix_sets_child_modes_and_cannot_be_relaxed(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        private = session_settings.Settings(
            agentId="companion",
            privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=True),
        )
        session_settings.save(
            store, sid, session_settings.Update(expected_revision=0, settings=private)
        )
        target = store.append_message(sid, "user", "Private source")
        public = private.model_copy(
            update={
                "privacy": session_settings.Privacy(memoryDisabled=False, harnessDisabled=False)
            }
        )
        session_settings.save(
            store, sid, session_settings.Update(expected_revision=1, settings=public)
        )
        body = request().model_copy(update={"expected_settings_revision": 2})
        child = forks.create(store, sid, target["id"], body)["session_id"]
        assert all(session_settings.load(store, child)["settings"]["privacy"].values())
        with pytest.raises(agents.AgentError, match="inherited"):
            session_settings.save(
                store, child, session_settings.Update(expected_revision=1, settings=public)
            )


def test_foreign_boundary_stale_review_and_summary_requirements(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        target = store.append_message(sid, "user", "Source")
        foreign = store.append_message(store.create_session(), "user", "Foreign")
        with pytest.raises(tasks.TaskError, match="this conversation"):
            forks.create(store, sid, foreign["id"], request())
        with pytest.raises(tasks.TaskError, match="changed"):
            forks.create(
                store,
                sid,
                target["id"],
                request().model_copy(update={"expected_context_revision": 1}),
            )
        with store._connect() as db:
            db.execute("UPDATE sessions SET summary='Older summary' WHERE id=?", (sid,))
        with pytest.raises(tasks.TaskError, match="summary"):
            forks.create(store, sid, target["id"], request())
        child = forks.create(store, sid, target["id"], request(reviewed_summary="Reviewed"))[
            "session_id"
        ]
        assert store.get_session(child)["summary"] == "Reviewed"


def test_owner_route_and_paginated_branch_history(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        first = store.append_message(sid, "user", "First")
        target = store.append_message(sid, "assistant", "Second")
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        client = TestClient(api.app)
        url = f"/sessions/{sid}/messages/{target['id']}/fork"
        assert client.post(url, json=request().model_dump()).status_code == 401
        headers = {"X-Pi-Key": "synthetic-owner"}
        result = client.post(url, json=request().model_dump(), headers=headers)
        assert result.status_code == 200, result.text
        child = result.json()["session_id"]
        page = client.get(f"/sessions/{child}/branch-history?limit=1", headers=headers).json()
        assert page["results"][0]["id"] == first["id"] and page["results"][0]["inherited"]
        page2 = client.get(
            f"/sessions/{child}/branch-history",
            params={"cursor": page["next_cursor"]},
            headers=headers,
        ).json()
        assert page2["results"][0]["id"] == target["id"] and page2["next_cursor"] is None


def test_fork_cannot_silently_drop_pin_after_boundary(tmp_path):
    from test_context_controls import policy

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        target = store.append_message(sid, "user", "Earlier")
        pin = store.append_message(sid, "assistant", "Pinned later")
        context_controls.save(
            store,
            sid,
            context_controls.Update(expected_revision=0, policy=policy({pin["id"]: "keep-exact"})),
        )
        with pytest.raises(tasks.TaskError, match="pins"):
            forks.create(
                store,
                sid,
                target["id"],
                request().model_copy(update={"expected_context_revision": 1}),
            )
