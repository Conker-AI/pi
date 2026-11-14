"""History by named chats: Home's "Jump back in" asks for the chats it remembers, not page 1 only."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation import history as session_history
from pi.conversation.store import Store
from pi.schedule.tasks import TaskError


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def populate(store, count=120):
    with store._connect() as db:
        db.executemany(
            "INSERT INTO sessions(id,title,status,created_at,summary) VALUES(?,?,'open',?,?)",
            [(f"ses_{i:04}", f"Title {i}", i, f"Summary {i}") for i in range(count)],
        )


def ids_of(result):
    return [row["id"] for row in result["results"]]


def test_ids_reach_chats_older_than_the_first_page(store):
    populate(store)
    assert "ses_0003" not in ids_of(session_history.page(store))
    result = session_history.page(store, ids=["ses_0003", "ses_0110", "ses_0003", "ses_9999"])
    # Newest first like the default view; repeats and unknown ids add nothing.
    assert ids_of(result) == ["ses_0110", "ses_0003"] and result["next_cursor"] is None
    assert result["results"][1] == next(
        row
        for row in session_history.page(store, sort="oldest")["results"]
        if row["id"] == "ses_0003"
    )


def test_ids_never_return_hidden_trashed_archived_or_forgotten_chats(store):
    populate(store)
    with store._connect() as db:
        db.execute("UPDATE sessions SET status='forgotten' WHERE id='ses_0001'")
        db.execute("INSERT INTO session_trash VALUES('ses_0002', 1)")
        db.execute("INSERT INTO session_archive VALUES('ses_0004', 1)")
        # A fork of a trashed chat is hidden with it.
        db.execute("UPDATE sessions SET parent_id='ses_0002' WHERE id='ses_0005'")
    named = ["ses_0001", "ses_0002", "ses_0004", "ses_0005", "ses_0006"]
    assert ids_of(session_history.page(store, ids=named)) == ["ses_0006"]
    # The default view still lists the forgotten chat as a tombstone.
    walked = session_history.page(store, sort="oldest")["results"]
    assert {"ses_0001"} <= {row["id"] for row in walked}
    assert ids_of(session_history.page(store, ids=named, archived="include")) == [
        "ses_0006",
        "ses_0004",
    ]
    with pytest.raises(TaskError) as refused:
        session_history.page(store, ids=named, trashed="only")
    assert refused.value.code == "invalid_history_request"


def test_ids_pages_hold_the_limit_and_bind_their_cursor(store):
    populate(store)
    named = [f"ses_{i:04}" for i in range(50)]
    first = session_history.page(store, ids=named, limit=20)
    assert len(first["results"]) == 20 and first["next_cursor"] is not None
    rest = session_history.page(store, ids=named, limit=50, cursor=first["next_cursor"])
    assert ids_of(first) + ids_of(rest) == [f"ses_{i:04}" for i in reversed(range(50))]
    # A cursor from one set of ids is not a cursor for another, nor for the default view.
    for other in ({"ids": named[:10]}, {}):
        with pytest.raises(TaskError) as refused:
            session_history.page(store, cursor=first["next_cursor"], **other)
        assert refused.value.code == "invalid_history_cursor"


@pytest.mark.parametrize("ids", [[], [f"ses_{i:04}" for i in range(51)], ["bad id"], [""], [7]])
def test_ids_bounded_and_validated(store, ids):
    with pytest.raises(TaskError) as refused:
        session_history.page(store, ids=ids)
    assert refused.value.code == "invalid_history_request"


def test_route_takes_comma_separated_ids_and_default_is_unchanged(store, monkeypatch):
    populate(store)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime-key").hexdigest(), raising=False
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Gateway-Key": "runtime-key"}
    default = client.get("/sessions/history", headers=headers)
    assert default.json() == session_history.page(store)
    assert len(default.json()["results"]) == 50
    named = client.get("/sessions/history?ids=ses_0002,ses_0119", headers=headers)
    assert named.status_code == 200 and ids_of(named.json()) == ["ses_0119", "ses_0002"]
    for query in ("ids=", "ids=ses_0001,", "ids=a&ids=b", "ids=" + ",".join(["ses_0001"] * 51)):
        refused = client.get("/sessions/history?" + query, headers=headers)
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "invalid_history_request"
