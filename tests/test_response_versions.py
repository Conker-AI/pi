from contextlib import closing

import pytest
from test_context_controls import policy
from test_loop import Recorder, loop_with

from pi import context_controls as context
from pi import message_forks, tasks
from pi import response_versions as versions
from pi.store import Store


def alternative(store, sid, original, text="Alternative answer"):
    tid = store.start_turn(sid)
    message = store.complete_turn(tid, text)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        root = versions.register(db, original, message["id"])
        db.commit()
    return root, message


def choose(store, sid, root, mid, revision=1):
    return versions.select(
        store, sid, root, versions.Selection(message_id=mid, expected_revision=revision)
    )


def test_selection_keeps_original_slot_and_preserves_transcript(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        original = loop_with(store, Recorder("Original answer")).run_turn(sid, "First question")[
            "message"
        ]
        later = store.append_message(sid, "user", "Later question")
        root, replacement = alternative(store, sid, original["id"])
        choose(store, sid, root, replacement["id"])
        assert [m["content"] for m in context.history(store, sid)] == [
            "First question",
            "Alternative answer",
            "Later question",
        ]
        assert len(store.messages(sid)) == 4
        provider = Recorder()
        loop_with(store, provider).run_turn(sid, "Next question")
        assert [m.content for m in provider.calls[0]] == [
            "First question",
            "Alternative answer",
            "Later question",
            "Next question",
        ]
        assert (
            store.get_message(original["id"])["response_family"]["selected_message_id"]
            == replacement["id"]
        )
        assert store.get_message(later["id"])["content"] == "Later question"
    with closing(Store(path)) as store:
        assert context.history(store, sid)[1]["id"] == replacement["id"]
        choose(store, sid, root, original["id"], 2)
        assert context.history(store, sid)[1]["id"] == original["id"]


def test_branch_freezes_selected_version_at_original_boundary(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        original = loop_with(store, Recorder()).run_turn(sid, "Question")["message"]
        store.append_message(sid, "user", "After boundary")
        root, replacement = alternative(store, sid, original["id"])
        choose(store, sid, root, replacement["id"])
        fork = message_forks.create(
            store,
            sid,
            replacement["id"],
            message_forks.Fork(
                request_id="version_fork_001",
                expected_settings_revision=0,
                expected_context_revision=0,
            ),
        )
        choose(store, sid, root, original["id"], 2)
        assert [r["content"] for r in context.history(store, fork["session_id"])] == [
            "Question",
            "Alternative answer",
        ]


def test_context_rules_and_busy_turn_prevent_silent_switch(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        original = loop_with(store, Recorder()).run_turn(sid, "Question")["message"]
        root, replacement = alternative(store, sid, original["id"])
        context.save(
            store, sid, context.Update(expected_revision=0, policy=policy({root: "keep-exact"}))
        )
        with pytest.raises(tasks.TaskError) as failure:
            choose(store, sid, root, replacement["id"])
        assert failure.value.detail["code"] == "version_context_conflict"
        with pytest.raises(context.ContextError):
            context.save(
                store,
                sid,
                context.Update(
                    expected_revision=1, policy=policy({replacement["id"]: "keep-exact"})
                ),
            )
        context.save(store, sid, context.Update(expected_revision=1, policy=policy({})))
        tid = store.start_turn(sid)
        with pytest.raises(tasks.TaskError) as failure:
            choose(store, sid, root, replacement["id"])
        assert failure.value.detail["code"] == "session_busy"
        store.finish_turn(tid, "cancelled")
        choose(store, sid, root, replacement["id"])
        with pytest.raises(tasks.TaskError):
            choose(store, sid, root, original["id"])


def test_cannot_join_foreign_answers_or_move_version_between_families(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid, other = store.create_session(), store.create_session()
        loop = loop_with(store, Recorder())
        original = loop.run_turn(sid, "One")["message"]
        foreign = loop.run_turn(other, "Other")["message"]
        with store._connect() as db, pytest.raises(tasks.TaskError):
            versions.register(db, original["id"], foreign["id"])
        root, replacement = alternative(store, sid, original["id"])
        second = loop.run_turn(sid, "Two")["message"]
        with store._connect() as db, pytest.raises(tasks.TaskError):
            versions.register(db, second["id"], replacement["id"])
        with pytest.raises(tasks.TaskError):
            choose(store, sid, root, foreign["id"])


def test_owner_api_and_forgetting_preserve_only_unavailable_identifiers(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pi import api, forgetting

    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        original = loop_with(store, Recorder()).run_turn(sid, "Private source")["message"]
        root, replacement = alternative(store, sid, original["id"])
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        client = TestClient(api.app)
        url = f"/sessions/{sid}/response-families/{root}"
        body = {"message_id": replacement["id"], "expected_revision": 1}
        assert client.post(url + "/select", json=body).status_code == 401
        headers = {"X-Pi-Key": "synthetic-owner"}
        response = client.post(url + "/select", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert len(client.get(url, headers=headers).json()["versions"]) == 2
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    with closing(Store(path)) as store:
        with pytest.raises(tasks.TaskError):
            choose(store, sid, root, original["id"], 2)
        assert "response_family" not in store.get_message(replacement["id"])
