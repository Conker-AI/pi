"""Archived chats leave the chat list and come back; the list filters and sorts them."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi.api import browser_contract
from pi.api import server as api
from pi.conversation import archive
from pi.conversation import history as session_history
from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store
from pi.core.errors import ServiceError
from pi.schedule.tasks import TaskError


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def chats(store, *titles):
    return [store.create_session(title=title) for title in titles]


def ids(store, **kwargs):
    rows, cursor = [], None
    while True:
        page = session_history.page(store, cursor=cursor, limit=1, **kwargs)
        rows.extend(row["id"] for row in page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows


def mark(store, sessions, archived=True):
    return archive.apply(store, archive.Archive(sessions=sessions, archived=archived))


def test_archived_chats_leave_the_default_list_and_come_back(store):
    a, b, c = chats(store, "a", "b", "c")
    assert mark(store, [a, c, a]) == {"sessions": [a, c], "archived": True}
    assert ids(store) == [b]
    assert ids(store, archived="only") == [c, a]
    assert ids(store, archived="include") == [c, b, a]
    rows = session_history.page(store, archived="include")["results"]
    assert [row["archived_at"] is not None for row in rows] == [True, False, True]
    mark(store, [c], archived=False)
    assert ids(store) == [c, b] and ids(store, archived="only") == [a]


def test_archiving_again_keeps_the_first_time_and_restoring_twice_is_harmless(store):
    (a,) = chats(store, "a")
    mark(store, [a])
    first = session_history.page(store, archived="only")["results"][0]["archived_at"]
    mark(store, [a])
    assert session_history.page(store, archived="only")["results"][0]["archived_at"] == first
    mark(store, [a], archived=False)
    mark(store, [a], archived=False)
    assert ids(store) == [a]


def test_one_missing_or_forgotten_chat_changes_nothing(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        a, gone = chats(store, "a", "gone")
    forget(path, gone, preview(path, gone)["confirmation"])
    with closing(Store(path)) as store:
        for batch in ([a, gone], [a, "ses_" + "0" * 16]):
            with pytest.raises(ServiceError) as error:
                mark(store, batch)
            assert error.value.status == 404
            assert ids(store, archived="only") == []
        mark(store, [a])
        assert ids(store, archived="only") == [a]


@pytest.mark.parametrize(
    "body",
    [
        {"sessions": [], "archived": True},
        {"sessions": ["ses_" + f"{i:016x}" for i in range(101)], "archived": True},
        {"sessions": ["ses_0123"], "archived": True},
        {"sessions": ["../x"], "archived": True},
        {"sessions": ["ses_0123456789abcdef"], "archived": "yes"},
        {"sessions": ["ses_0123456789abcdef"]},
        {"sessions": ["ses_0123456789abcdef"], "archived": True, "all": True},
    ],
)
def test_archive_body_is_exact(body):
    with pytest.raises(ValidationError):
        archive.Archive.model_validate(body)


def test_sort_orders_cover_every_chat_once(store):
    store.create_session(title="")
    b, z, a2, a1 = chats(store, "banana", "Zebra", "apple", "Apple")
    untitled = session_history.page(store, sort="oldest")["results"][0]["id"]
    newest = ids(store)
    assert ids(store, sort="oldest") == list(reversed(newest))
    # Name order reads like people do (case ignored), ties by id, untitled chats last.
    by_name = ids(store, sort="name")
    assert by_name[:2] == sorted([a1, a2]) and by_name[2:] == [b, z, untitled]


def test_a_cursor_belongs_to_its_filter_and_order(store):
    chats(store, "a", "b", "c")
    cursor = session_history.page(store, limit=1, sort="name")["next_cursor"]
    for other in ({"sort": "newest"}, {"sort": "oldest"}, {"sort": "name", "archived": "include"}):
        with pytest.raises(TaskError) as error:
            session_history.page(store, limit=1, cursor=cursor, **other)
        assert error.value.detail["code"] == "invalid_history_cursor"
    # The default view keeps the cursors it issued before archiving existed.
    plain = session_history.page(store, limit=1)["next_cursor"]
    assert plain.split(".")[1] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("kwargs", [{"archived": "all"}, {"sort": "recent"}, {"sort": None}])
def test_unknown_filter_or_order_is_refused_before_read(store, monkeypatch, kwargs):
    monkeypatch.setattr(store, "_connect", lambda: pytest.fail("read before validation"))
    with pytest.raises(TaskError) as error:
        session_history.page(store, **kwargs)
    assert error.value.detail["code"] == "invalid_history_request"


def test_search_combines_with_filter_and_order(store):
    a, b, _ = chats(store, "trip north", "trip south", "groceries")
    mark(store, [a])
    assert ids(store, q="trip") == [b]
    assert ids(store, q="trip", archived="include", sort="name") == [a, b]


def test_api_route_is_owner_only_and_history_takes_the_new_parameters(store, monkeypatch):
    (a,) = chats(store, "a")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime-key").hexdigest(), raising=False
    )
    client = TestClient(api.app)
    body = {"sessions": [a], "archived": True}
    assert not browser_contract.runtime_allowed("POST", "/sessions/archive")
    assert browser_contract.owner_allowed("POST", "/sessions/archive")
    assert browser_contract.session_only_write("POST", "/sessions/archive")
    assert client.post("/sessions/archive", json=body).status_code == 401
    runtime = {"X-Pi-Gateway-Key": "runtime-key"}
    assert client.post("/sessions/archive", json=body, headers=runtime).status_code in (401, 403)
    answer = client.post("/sessions/archive", json=body, headers={"X-Pi-Key": "recovery-key"})
    assert answer.status_code == 200 and answer.json() == {"sessions": [a], "archived": True}
    listed = client.get("/sessions/history?archived=only&sort=name", headers=runtime)
    assert listed.status_code == 200 and [row["id"] for row in listed.json()["results"]] == [a]
    for query in ("archived=everything", "sort=activity", "sort=name&sort=oldest"):
        refused = client.get("/sessions/history?" + query, headers=runtime)
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "invalid_history_request"
