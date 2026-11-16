"""R-U3 step 4: GET /memory/recall-notices lists the open "answers used a memory you changed"
notices for the dashboard's Inbox card. Ids, counts and chat titles only, owner only, and only
chats the owner may see (the same rule as the Inbox and GET /memory/{id}/uses)."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient
from test_memory_recall_notice import (
    KEY,
    OWNER,
    WORDS,
    Remote,
    correct,
    items,
    use,
)

from pi.api.browser_contract import owner_allowed
from pi.conversation import archive, recall_notices
from pi.conversation.forgetting import forget as forget_chat
from pi.conversation.forgetting import preview as preview_forget
from pi.conversation.store import Store
from pi.memory import forget as memory_forget
from pi.memory.corrections import Client

ADMIN = {"X-Pi-Key": "synthetic-admin"}
OWNER_HEADER = {"X-Pi-Owner-Key": OWNER}
PATH = "/memory/recall-notices"


@pytest.fixture
def setup(tmp_path):
    remote = Remote()
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        closing(
            Client(
                "http://memorygate", KEY, "companion", transport=httpx.MockTransport(remote.handle)
            )
        ) as client,
    ):
        yield store, client, remote


@pytest.fixture
def api(setup, monkeypatch):
    from pi.api import server

    store, _, _ = setup
    monkeypatch.setattr(server.app.state, "store", store, raising=False)
    monkeypatch.setattr(server.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        server.app.state,
        "owner_key_hash",
        hashlib.sha256(OWNER.encode()).hexdigest(),
        raising=False,
    )
    return TestClient(server.app)


def listed(api, headers=ADMIN):
    response = api.get(PATH, headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()
    recall_notices.RecallNoticePage.model_validate(page)  # what it sends is what it describes
    return page["items"]


def test_lists_the_notice_with_counts_and_chat_titles_and_no_words(setup, api):
    store, client, _ = setup
    named, unnamed = store.create_session(title="Holiday plans"), store.create_session()
    use(store, named)
    use(store, named)
    use(store, unnamed)
    assert listed(api) == []
    correct(store, client)
    [item] = listed(api)
    assert item["kind"] == "memory-recall" and item["change"] == "corrected"
    assert (item["answers"], item["chats"], item["memoryId"]) == (3, 2, "mem-1")
    assert item["title"] == "A memory you corrected was used in 3 answers in 2 chats"
    assert {chat["id"]: chat["title"] for chat in item["sessions"]} == {
        named: "Holiday plans",
        unnamed: "",
    }
    assert item["sessionIds"] == [chat["id"] for chat in item["sessions"]]
    assert item["source"] == {
        "kind": "memory-recall",
        "noticeId": item["id"],
        "memoryId": "mem-1",
    }
    assert WORDS not in json.dumps(item) and "new words" not in json.dumps(item)
    # The Inbox reads the same notice, so the two never disagree.
    assert items(store) == [item]


def test_a_forget_is_listed_as_forgotten(setup, api):
    store, client, _ = setup
    use(store, store.create_session())
    body = memory_forget.Forget(
        request_id="forget_request_0001", memory_id="mem-1", expected_revision=3
    )
    memory_forget.forget(store, client, body)
    [item] = listed(api)
    assert item["change"] == "forgotten"
    assert item["title"] == "A memory you forgot was used in 1 answer in 1 chat"


def test_only_the_owner_reads_it_and_the_browser_contract_allows_the_read(setup, api):
    store, client, _ = setup
    use(store, store.create_session())
    correct(store, client)
    assert owner_allowed("GET", PATH)
    assert not owner_allowed("POST", PATH)
    assert not owner_allowed("GET", PATH + "/mrn_" + "0" * 32)
    assert api.get(PATH).status_code in {401, 403}
    assert api.get(PATH, headers={"X-Pi-Key": "wrong"}).status_code in {401, 403}
    assert api.get(PATH, headers={"X-Pi-Owner-Key": "wrong"}).status_code in {401, 403}
    assert len(listed(api, OWNER_HEADER)) == 1 and len(listed(api, ADMIN)) == 1


def test_a_trashed_chat_drops_out_and_a_restored_one_returns(setup, api):
    store, client, _ = setup
    kept, trashed = store.create_session(title="Kept"), store.create_session(title="Trashed")
    use(store, kept)
    use(store, trashed)
    correct(store, client)
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    [item] = listed(api)
    assert (item["answers"], item["chats"]) == (1, 1)
    assert item["sessionIds"] == [kept]
    assert "Trashed" not in json.dumps(item)
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=False))
    [item] = listed(api)
    assert {chat["title"] for chat in item["sessions"]} == {"Kept", "Trashed"}


def test_a_notice_whose_chats_are_all_gone_is_not_listed(setup, api):
    store, client, _ = setup
    only = store.create_session(title="Secret plans")
    use(store, only)
    correct(store, client)
    assert len(listed(api)) == 1
    archive.trash(store, archive.Trash(sessions=[only], trashed=True))
    assert listed(api) == []
    assert api.get(PATH, headers=ADMIN).text.count("Secret plans") == 0


def test_a_chat_made_from_a_trashed_chat_is_hidden_with_it(setup, api):
    store, client, _ = setup
    parent = store.create_session(title="Parent chat")
    fork = store.create_session(title="Fork of the parent", parent_id=parent)
    use(store, fork)
    correct(store, client)
    assert [chat["id"] for chat in listed(api)[0]["sessions"]] == [fork]
    archive.trash(store, archive.Trash(sessions=[parent], trashed=True))
    assert listed(api) == []
    archive.trash(store, archive.Trash(sessions=[parent], trashed=False))
    assert [chat["title"] for chat in listed(api)[0]["sessions"]] == ["Fork of the parent"]


def test_a_forgotten_chat_is_never_listed_or_counted(tmp_path, monkeypatch):
    from pi.api import server

    path = tmp_path / "pi.db"
    remote = Remote()
    transport = httpx.MockTransport(remote.handle)
    with closing(Client("http://memorygate", KEY, "companion", transport=transport)) as client:
        with closing(Store(path)) as store:
            kept = store.create_session(title="Kept chat")
            gone = store.create_session(title="Forgotten chat")
            use(store, kept)
            use(store, gone)
            correct(store, client)
        forget_chat(path, gone, preview_forget(path, gone)["confirmation"])
        with closing(Store(path)) as store:
            monkeypatch.setattr(server.app.state, "store", store, raising=False)
            monkeypatch.setattr(server.app.state, "admin_key", "synthetic-admin", raising=False)
            [item] = TestClient(server.app).get(PATH, headers=ADMIN).json()["items"]
            assert (item["answers"], item["chats"], item["sessionIds"]) == (1, 1, [kept])
            assert "Forgotten chat" not in json.dumps(item)


def test_a_dismissed_notice_is_not_listed_and_dismissing_needs_the_owner(setup, api):
    store, client, _ = setup
    use(store, store.create_session())
    correct(store, client)
    [item] = listed(api)
    path = f"{PATH}/{item['id']}/dismiss"
    assert api.post(path).status_code in {401, 403}
    assert len(listed(api)) == 1
    assert api.post(path, headers=OWNER_HEADER).status_code == 200
    assert listed(api) == []
    # The uses stay readable: dismissing hides the card, not the record.
    assert api.get("/memory/mem-1/uses", headers=ADMIN).json()["total"] == 1


def test_a_second_notice_for_new_answers_is_listed_newest_first(setup, api):
    store, client, remote = setup
    use(store, store.create_session(title="First"), revision=3)
    correct(store, client)
    remote.revision = 4
    later = store.create_session(title="Later")
    use(store, later, revision=4, memory="mem-2")
    with store._connect() as db:
        recall_notices.file(
            db,
            request_id="second_request_00001",
            memory_id="mem-2",
            change="corrected",
            revision=4,
            now=4_000_000_000.0,
        )
    page = listed(api)
    assert [item["memoryId"] for item in page] == ["mem-2", "mem-1"]  # newest first
    assert recall_notices.listing(store)["items"] == page
    assert page[0]["sessions"] == [{"id": later, "title": "Later"}]


def test_chats_are_capped_and_titles_stay_with_their_ids(setup, api):
    store, client, _ = setup
    made = [store.create_session(title=f"Chat {n}") for n in range(recall_notices.MAX_CHATS + 5)]
    for session in made:
        use(store, session)
    correct(store, client)
    [item] = listed(api)
    assert item["chats"] == len(made) and item["answers"] == len(made)
    assert len(item["sessionIds"]) == recall_notices.MAX_CHATS == len(item["sessions"])
    titles = {session: f"Chat {n}" for n, session in enumerate(made)}
    assert all(titles[chat["id"]] == chat["title"] for chat in item["sessions"])


def test_the_route_is_described_and_does_not_clash_with_the_uses_route(setup, api):
    store, client, _ = setup
    use(store, store.create_session())
    correct(store, client)
    operation = api.get("/openapi.json").json()["paths"][PATH]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/RecallNoticePage"}
    # A memory that happens to be called "recall-notices" still reads its uses, not this list.
    assert api.get("/memory/recall-notices/uses", headers=ADMIN).json()["memoryId"] == (
        "recall-notices"
    )
