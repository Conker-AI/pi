"""R-U3 step 2: GET /memory/{id}/uses lists the turns that used one memory (ids and chat titles,
no words), newest first, paged, hiding forgotten and trashed chats."""

from __future__ import annotations

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api.browser_contract import owner_allowed
from pi.conversation import archive
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory import uses

ADMIN = {"X-Pi-Key": "synthetic-admin"}
OWNER = "synthetic-owner"
PACKAGE = {"memories": [{"id": "mem_1", "text": "private words", "revision": 2}, {"id": "mem_2"}]}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


@pytest.fixture
def client(store, monkeypatch):
    from pi.api import server as api

    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    return TestClient(api.app)


def use(store, session, package=PACKAGE):
    store.append_message(session, "user", "hello")
    turn = store.start_turn(session)
    memory_store.save_context(store, turn, "ok", package)
    return turn


def get(client, memory="mem_1", query="", headers=ADMIN):
    return client.get(f"/memory/{memory}/uses{query}", headers=headers)


def test_lists_each_turn_newest_first_with_chat_titles_and_counts(store, client):
    first, second = store.create_session(title="Plans"), store.create_session()
    turns = [use(store, first), use(store, first), use(store, second)]
    use(store, second, {"memories": [{"id": "mem_9"}]})  # another memory
    page = get(client).json()
    uses.UsePage.model_validate(page)
    assert [i["turnId"] for i in page["items"]] == turns[::-1]
    assert page["total"] == 3 and page["chats"] == 2 and page["nextAfter"] is None
    assert page["items"][-1]["sessionTitle"] == "Plans" and page["items"][0]["sessionTitle"] == ""
    assert {i["revision"] for i in page["items"]} == {2} and page["since"] <= page["items"][-1][
        "at"
    ]
    assert "private words" not in get(client).text


def test_pages_with_a_cursor_and_refuses_a_bad_one(store, client):
    session = store.create_session()
    turns = [use(store, session) for _ in range(5)]
    seen, after = [], ""
    while True:
        page = get(client, query=f"?limit=2{after}").json()
        seen += [i["turnId"] for i in page["items"]]
        if not page["nextAfter"]:
            break
        after = "&after=" + page["nextAfter"]
    assert seen == turns[::-1]
    for bad in ("nope", "1.5|x|other", "1.5|bad id|recall"):
        assert get(client, query=f"?after={bad}").status_code == 422


def test_a_trashed_or_forgotten_chat_is_hidden_and_returns_on_restore(store, client):
    kept, trashed = store.create_session(), store.create_session()
    use(store, kept)
    use(store, trashed)
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    page = get(client).json()
    assert page["total"] == 1 and {i["sessionId"] for i in page["items"]} == {kept}
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=False))
    assert get(client).json()["total"] == 2
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE sessions SET status='forgotten' WHERE id=?", (trashed,))
        db.commit()
    assert get(client).json()["total"] == 1


def test_a_memory_with_no_uses_is_an_empty_page_and_a_bad_id_is_refused(store, client):
    page = get(client, "mem_unknown").json()
    assert page["items"] == [] and page["total"] == 0 and page["since"] is None
    assert get(client, "bad%20id").status_code == 422
    assert get(client, "x" * 201).status_code in (404, 422)


def test_the_owner_may_read_it_and_the_reply_is_described(client):
    owner = {"X-Pi-Owner-Key": OWNER}
    assert get(client, headers=owner).status_code == 200
    assert get(client, headers={}).status_code == 401
    assert owner_allowed("GET", "/memory/mem_1/uses") and not owner_allowed(
        "POST", "/memory/mem_1/uses"
    )
    paths = client.get("/openapi.json").json()["paths"]
    reply = paths["/memory/{memory_id}/uses"]["get"]["responses"]["200"]["content"]
    assert reply["application/json"]["schema"] == {"$ref": "#/components/schemas/UsePage"}
