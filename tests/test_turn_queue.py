"""Durable queued choices must not silently change or execute on lifecycle edits."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_attachment_turns import upload

from pi import api, attachments, forgetting, session_settings, tasks
from pi import turn_queue as q
from pi.store import Store


def body(number=0, text="Next question", attachment_ids=None):
    return q.Enqueue(
        request_id=f"queue_request_{number:04d}", text=text, attachment_ids=attachment_ids or []
    )


def test_queue_survives_reopen_preserves_order_and_caps_at_five(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        for i in range(5):
            entry = q.enqueue(store, sid, body(i))
            assert q.enqueue(store, sid, body(i)) == entry
        with pytest.raises(tasks.TaskError, match="five"):
            q.enqueue(store, sid, body(5))
        assert store.messages(sid) == []
    with closing(Store(path)) as store:
        value = q.read(store, sid)
        assert [entry["id"] for entry in value["entries"]] == [body(i).request_id for i in range(5)]
        assert not value["paused"]
        with pytest.raises(tasks.TaskError, match="already used"):
            q.enqueue(store, sid, body(0, "changed"))


def test_edit_remove_revision_guards_and_no_recreation(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        entry = q.enqueue(store, sid, body())
        edited = q.change(
            store, sid, entry["id"], q.Edit(expected_revision=1, text="Edited"), "edit"
        )
        assert edited["payload"]["text"] == "Edited"
        assert edited["selection"] == entry["selection"]
        with pytest.raises(tasks.TaskError, match="changed"):
            q.change(store, sid, entry["id"], q.Revision(expected_revision=1), "remove")
        removed = q.change(store, sid, entry["id"], q.Revision(expected_revision=2), "remove")
        assert removed["payload"] is None and removed["selection"] is None
        assert q.read(store, sid)["entries"] == []
        assert q.enqueue(store, sid, body())["state"] == "removed"


def test_changed_privacy_requires_review_and_resume_does_not_refresh(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        entry = q.enqueue(store, sid, body())
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=True),
                ),
            ),
        )
        value = q.read(store, sid)
        assert value["paused"] and value["reason"] == "queue_review_required"
        with pytest.raises(tasks.TaskError, match="review"):
            q.pause(store, sid, q.QueueRevision(expected_revision=value["revision"]), False)
        reviewed = q.change(store, sid, entry["id"], q.Revision(expected_revision=1), "review")
        assert reviewed["selection"]["execution"]["privacy"]["memoryDisabled"]
        value = q.read(store, sid)
        assert value["paused"]
        assert not q.pause(store, sid, q.QueueRevision(expected_revision=value["revision"]), False)[
            "paused"
        ]


def test_removed_attachment_pauses_queue_without_dropping_text(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        identity = upload(store, sid)
        q.enqueue(store, sid, body(attachment_ids=[identity]))
        attachments.remove(store, sid, identity, resolve=session_settings.source_privacy)
        value = q.read(store, sid)
        assert value["paused"] and value["entries"][0]["payload"]["text"] == "Next question"


def test_forgetting_scrubs_queued_payloads_and_snapshots(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        q.enqueue(store, sid, body(text="PRIVATE_QUEUE_MARKER_001"))
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    assert b"PRIVATE_QUEUE_MARKER_001" not in path.read_bytes()
    with closing(Store(path)) as store, pytest.raises(tasks.TaskError):
        q.read(store, sid)


def test_owner_routes_do_not_dispatch(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-owner", raising=False)
        client = TestClient(api.app)
        url = f"/sessions/{sid}/queue"
        assert client.get(url).status_code == 401
        headers = {"X-Pi-Key": "synthetic-owner"}
        assert client.post(url, json=body().model_dump(), headers=headers).status_code == 200
        value = client.get(url, headers=headers).json()
        response = client.post(
            url + "/pause", json={"expected_revision": value["revision"]}, headers=headers
        )
        assert response.status_code == 200 and response.json()["paused"]
        assert store.messages(sid) == []
