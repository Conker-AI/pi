"""Chats in the trash leave every list, search and recall, and come back when restored."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi.api import browser_contract
from pi.api import server as api
from pi.context import universal_search
from pi.conversation import archive
from pi.conversation import history as session_history
from pi.conversation import search as conversation_search
from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store
from pi.memory.service import _without_trashed


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def ids(store, **kwargs):
    return [row["id"] for row in session_history.page(store, **kwargs)["results"]]


def trash(store, sessions, trashed=True):
    return archive.trash(store, archive.Trash(sessions=sessions, trashed=trashed))


def test_trashed_chats_leave_every_list_and_restoring_brings_them_back(store):
    a, b, c = (store.create_session(title=t) for t in ("a", "b", "c"))
    archive.apply(store, archive.Archive(sessions=[c], archived=True))
    assert trash(store, [a, c, a]) == {"sessions": [a, c], "trashed": True}
    assert ids(store) == [b]
    assert ids(store, archived="only") == [] and ids(store, archived="include") == [b]
    # The trash lists them whatever their archive state, newest first.
    assert ids(store, trashed="only") == [c, a]
    assert all(r["trashed_at"] for r in session_history.page(store, trashed="only")["results"])
    trash(store, [a, c], trashed=False)
    assert ids(store) == [b, a] and ids(store, archived="only") == [c]
    assert ids(store, trashed="only") == []


def test_trashed_chats_are_hidden_from_search_and_recall(store):
    kept, gone = (store.create_session(title=f"trip {n}") for n in ("kept", "gone"))
    says = {sid: store.append_message(sid, "user", "pack the tent")["id"] for sid in (kept, gone)}
    trash(store, [gone])
    found = conversation_search.search(store, "tent")["results"]
    assert [row["session_id"] for row in found] == [kept]
    titles = [row["recordId"] for row in universal_search.conversation_metadata(store, "trip")[0]]
    assert gone not in titles and kept in titles
    assert not universal_search.public_session(store, gone)
    assert universal_search.public_session(store, kept)
    package = {
        "memories": [
            {"id": "by-session", "citations": [{"session_id": gone}]},
            {"id": "by-message", "citations": [{"message_id": says[gone]}]},
            {"id": "kept", "citations": [{"session_id": kept, "message_id": says[kept]}]},
        ],
        "entities": [{"id": "uncited"}, {"id": "odd", "citations": [{"session_id": [gone]}]}],
    }
    shown = _without_trashed(store, package)
    assert [m["id"] for m in shown["memories"]] == ["kept"]
    assert shown["entities"] == package["entities"]  # malformed citations hide nothing
    trash(store, [gone], trashed=False)
    assert _without_trashed(store, package) == package


def test_deleting_a_trashed_chat_forever_clears_its_trash_mark(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        (gone,) = (store.create_session(title="gone"),)
        trash(store, [gone])
    forget(path, gone, preview(path, gone)["confirmation"])
    with closing(Store(path)) as store, store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM session_trash").fetchone()[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"sessions": [], "trashed": True},
        {"sessions": ["ses_" + f"{i:016x}" for i in range(101)], "trashed": True},
        {"sessions": ["../x"], "trashed": True},
        {"sessions": ["ses_0123456789abcdef"]},
        {"sessions": ["ses_0123456789abcdef"], "trashed": "yes"},
    ],
)
def test_trash_body_is_exact(body):
    with pytest.raises(ValidationError):
        archive.Trash.model_validate(body)


def test_trash_is_an_owner_write_without_a_password(store, monkeypatch):
    (a,) = (store.create_session(title="a"),)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime-key").hexdigest(), raising=False
    )
    assert browser_contract.owner_allowed("POST", "/sessions/trash")
    assert browser_contract.session_only_write("POST", "/sessions/trash")
    assert not browser_contract.password_write("POST", "/sessions/trash")
    assert not browser_contract.runtime_allowed("POST", "/sessions/trash")
    client = TestClient(api.app)
    body = {"sessions": [a], "trashed": True}
    assert client.post("/sessions/trash", json=body).status_code == 401
    runtime = {"X-Pi-Gateway-Key": "runtime-key"}
    assert client.post("/sessions/trash", json=body, headers=runtime).status_code in (401, 403)
    answer = client.post("/sessions/trash", json=body, headers={"X-Pi-Key": "recovery-key"})
    assert answer.status_code == 200 and answer.json() == body
    listed = client.get("/sessions/history?trashed=only", headers=runtime)
    assert [row["id"] for row in listed.json()["results"]] == [a]
    refused = client.get("/sessions/history?trashed=include", headers=runtime)
    assert refused.status_code == 422
