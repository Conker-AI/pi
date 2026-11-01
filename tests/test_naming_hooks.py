"""Chat names after a turn: the after_turn hook, the title routes' keys, and turn scheduling."""

import hashlib
from contextlib import closing
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation import naming
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from tests.test_naming import Recording, configured

ADMIN = "synthetic-admin"
OWNER = "naming_test_owner_key"


def test_after_turn_names_unnamed_chat_once(tmp_path):
    adapter = Recording('{"title": "Auto named"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        naming.after_turn(store, {"one": adapter}, sid)
        loaded = naming.load(store, sid)
    assert loaded["title"] == "Auto named"
    assert loaded["source"] == "auto"


def test_after_turn_does_nothing_when_owner_name_exists(tmp_path):
    adapter = Recording('{"title": "Should not apply"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        naming.rename(store, sid, naming.Rename(expected_revision=0, title="Owner name"))
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        naming.after_turn(store, {"one": adapter}, sid)
        loaded = naming.load(store, sid)
    assert loaded["title"] == "Owner name"
    assert loaded["source"] == "owner"
    assert adapter.calls == []


def test_after_turn_does_nothing_when_auto_name_exists(tmp_path):
    adapter1 = Recording('{"title": "First auto"}')
    adapter2 = Recording('{"title": "Second auto"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        naming.after_turn(store, {"one": adapter1}, sid)
        # Second call should do nothing
        naming.after_turn(store, {"one": adapter2}, sid)
        loaded = naming.load(store, sid)
    assert loaded["title"] == "First auto"
    assert adapter2.calls == []


def test_after_turn_does_nothing_when_non_empty_created_title(tmp_path):
    adapter = Recording('{"title": "Should not apply"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session(title="Created title")
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        naming.after_turn(store, {"one": adapter}, sid)
        loaded = naming.load(store, sid)
    assert loaded["title"] == "Created title"
    assert adapter.calls == []


def test_after_turn_does_nothing_when_helpers_off(tmp_path):
    adapter = Recording('{"title": "Should not apply"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings={
                    "agentId": "companion",
                    "privacy": {"memoryDisabled": False, "harnessDisabled": True},
                },
            ),
        )
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        naming.after_turn(store, {"one": adapter}, sid)
    assert adapter.calls == []


def test_after_turn_does_nothing_when_no_configuration(tmp_path):
    adapter = Recording('{"title": "Should not apply"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        naming.after_turn(store, {"one": adapter}, sid)
    assert adapter.calls == []


def test_after_turn_never_raises_when_adapter_fails(tmp_path):
    class FailingAdapter:
        def complete_bounded(self, messages, *, model, timeout):
            raise RuntimeError("boom")

    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        # Should not raise
        naming.after_turn(store, {"one": FailingAdapter()}, sid)


# --- 8. HTTP routes ---


@pytest.fixture
def http(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(OWNER.encode()).hexdigest(),
            raising=False,
        )
        if not hasattr(api.app.state, "router") or api.app.state.router is None:
            from unittest.mock import MagicMock

            router_mock = MagicMock()
            router_mock.adapters.return_value = {}
            monkeypatch.setattr(api.app.state, "router", router_mock, raising=False)
        yield store, TestClient(api.app)


def test_title_routes_accept_admin_key(http):
    store, client = http
    sid = store.create_session()
    admin = {"X-Pi-Key": ADMIN}
    # GET
    response = client.get(f"/sessions/{sid}/title", headers=admin)
    assert response.status_code == 200
    # POST rename
    response = client.post(
        f"/sessions/{sid}/title",
        headers=admin,
        json={"expected_revision": 0, "title": "Renamed"},
    )
    assert response.status_code == 200
    # POST auto
    response = client.post(
        f"/sessions/{sid}/title/auto",
        headers=admin,
        json={"expected_revision": 1},
    )
    # May fail (no messages/config) but should not be 401/403
    assert response.status_code not in (401, 403)


def test_title_routes_take_the_owner_key_for_a_chat_id_only(http):
    store, client = http
    sid = store.create_session()
    owner = {"X-Pi-Owner-Key": OWNER}
    assert client.get(f"/sessions/{sid}/title", headers=owner).status_code == 200
    renamed = client.post(
        f"/sessions/{sid}/title", headers=owner, json={"expected_revision": 0, "title": "Named"}
    )
    assert renamed.status_code == 200
    assert client.post(f"/sessions/{sid}/title", headers=owner, json={}).status_code == 422
    assert client.get("/sessions/ses_abc/title", headers=owner).status_code == 403


def test_title_routes_refuse_no_key(http):
    store, client = http
    sid = store.create_session()
    assert client.get(f"/sessions/{sid}/title").status_code == 401
    assert client.post(f"/sessions/{sid}/title", json={}).status_code == 401
    assert client.post(f"/sessions/{sid}/title/auto", json={}).status_code == 401


def test_successful_turn_schedules_after_turn(tmp_path, monkeypatch):
    """A successful POST /sessions/{id}/turns schedules after_turn."""
    recorded_calls = []

    def fake_after_turn(store, providers, sid):
        recorded_calls.append(sid)

    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(OWNER.encode()).hexdigest(),
            raising=False,
        )

        from unittest.mock import MagicMock

        loop_mock = MagicMock()
        loop_mock.run_turn.return_value = {
            "session_id": "ses_test123",
            "turn_id": "trn_test123",
            "status": "complete",
            "acted": False,
            "message": {"id": "msg_test", "role": "assistant", "content": "Hi"},
            "memory": {},
        }
        monkeypatch.setattr(api.app.state, "loop", loop_mock, raising=False)
        router_mock = MagicMock()
        router_mock.adapters.return_value = {}
        monkeypatch.setattr(api.app.state, "router", router_mock, raising=False)

        with patch("pi.conversation.naming.after_turn", side_effect=fake_after_turn):
            client = TestClient(api.app)
            response = client.post(
                "/sessions/ses_test123/turns",
                headers={"X-Pi-Key": ADMIN},
                json={"text": "Hello"},
            )

    assert response.status_code == 200
    assert recorded_calls == ["ses_test123"]


def test_failed_turn_does_not_schedule_after_turn(tmp_path, monkeypatch):
    """A failed turn does not schedule after_turn."""
    recorded_calls = []

    def fake_after_turn(store, providers, sid):
        recorded_calls.append(sid)

    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(OWNER.encode()).hexdigest(),
            raising=False,
        )

        from unittest.mock import MagicMock

        from pi.turns.failures import TurnFailed

        loop_mock = MagicMock()
        loop_mock.run_turn.side_effect = TurnFailed("provider error", "trn_test")
        loop_mock.memory = MagicMock()
        loop_mock.memory.status.return_value = {}
        monkeypatch.setattr(api.app.state, "loop", loop_mock, raising=False)

        router_mock = MagicMock()
        router_mock.adapters.return_value = {}
        monkeypatch.setattr(api.app.state, "router", router_mock, raising=False)

        with patch("pi.conversation.naming.after_turn", side_effect=fake_after_turn):
            client = TestClient(api.app)
            response = client.post(
                "/sessions/ses_test123/turns",
                headers={"X-Pi-Key": ADMIN},
                json={"text": "Hello"},
            )

    assert response.status_code == 503
    assert recorded_calls == []
