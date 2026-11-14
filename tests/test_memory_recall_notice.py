"""R-U3 step 3: finishing a memory correction or forget files an Inbox notice "answers used a
memory you changed". It holds ids only, counts its reach when read in chats that are visible, and
is hidden once nothing visible is left."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from pi.agents.inbox import inbox
from pi.api.browser_contract import owner_allowed
from pi.conversation import archive, recall_notices
from pi.conversation.forgetting import forget as forget_chat
from pi.conversation.forgetting import preview as preview_forget
from pi.conversation.store import Store
from pi.memory import edits
from pi.memory import forget as memory_forget
from pi.memory import store as memory_store
from pi.memory.corrections import Client

KEY = "synthetic-correction-capability-" + "k" * 32
WORDS = "private memory words of the owner"
ADMIN = {"X-Pi-Key": "synthetic-admin"}
OWNER = "synthetic-owner"


class Remote:
    """MemoryGate's correction and forget routes, enough for one memory."""

    def __init__(self):
        self.revision = 3
        self.refuse = None

    def handle(self, request):
        identity = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and "/memories/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": identity,
                    "agent_id": "companion",
                    "revision": self.revision,
                    "text": WORDS,
                    "source_type": "inferred",
                    "confidence": "low",
                },
            )
        body = json.loads(request.content)
        if self.refuse:
            return httpx.Response(self.refuse, json={})
        if "/forget/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "request_id": identity,
                    "memory_id": body["memory_id"],
                    "agent_id": "companion",
                    "revision": body["expected_revision"],
                    "status": "forgotten",
                    "index_removal": "removed",
                },
            )
        digest = edits.remote_digest("companion", edits.Apply(request_id=identity, **body))
        self.revision += 1
        return httpx.Response(
            200,
            json={
                "request_id": identity,
                "memory_id": body["memory_id"],
                "agent_id": "companion",
                "previous_revision": self.revision - 1,
                "revision": self.revision,
                "status": "applied",
                "payload_hash": digest,
                "indexing": "indexed",
            },
        )


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


def use(store, session, revision=3, memory="mem-1"):
    store.append_message(session, "user", "hello")
    turn = store.start_turn(session)
    package = {"memories": [{"id": memory, "text": WORDS, "revision": revision}]}
    memory_store.save_context(store, turn, "ok", package)
    store.finish_turn(turn, "complete")
    return turn


def correct(store, client, request="correction_request_0001", revision=3):
    body = edits.Apply(
        request_id=request, memory_id="mem-1", expected_revision=revision, text="new words"
    )
    return edits.apply(store, client, body)


def items(store):
    return [i for i in inbox(store)["needsYou"] if i["kind"] == "memory-recall"]


def stored(store):
    with store._connect() as db:
        return [
            tuple(r) for r in db.execute("SELECT title, detail, session_id, facts FROM notices")
        ]


def test_a_correction_files_a_notice_with_counts_and_no_words(setup):
    store, client, _ = setup
    first, second = store.create_session(), store.create_session()
    use(store, first)
    use(store, first)
    use(store, second)
    assert items(store) == []
    assert correct(store, client)["status"] == "applied"
    [item] = items(store)
    assert (item["answers"], item["chats"], item["change"], item["memoryId"]) == (
        3,
        2,
        "corrected",
        "mem-1",
    )
    assert item["title"] == "A memory you corrected was used in 3 answers in 2 chats"
    assert set(item["sessionIds"]) == {first, second}
    rows = stored(store)
    assert len(rows) == 1 and rows[0][2] is None
    dumped = json.dumps(rows)
    assert WORDS not in dumped and first not in dumped and second not in dumped
    assert json.loads(rows[0][3]) == {"change": "corrected", "memoryId": "mem-1", "revision": 3}


def test_a_forget_files_a_notice(setup):
    store, client, _ = setup
    use(store, store.create_session())
    body = memory_forget.Forget(
        request_id="forget_request_0001", memory_id="mem-1", expected_revision=3
    )
    memory_forget.forget(store, client, body)
    [item] = items(store)
    assert (item["change"], item["answers"], item["chats"]) == ("forgotten", 1, 1)
    assert item["title"] == "A memory you forgot was used in 1 answer in 1 chat"


def test_no_use_files_nothing_and_a_refused_correction_files_nothing(setup):
    store, client, remote = setup
    correct(store, client, request="correction_request_0002")
    assert stored(store) == []
    use(store, store.create_session())
    remote.refuse = 409
    with pytest.raises(Exception):  # noqa: B017  (the refusal's own error type is not the point)
        correct(store, client, request="correction_request_0003")
    assert stored(store) == [] and items(store) == []


def test_only_answers_that_saw_the_old_text_count(setup):
    store, client, _ = setup
    old = store.create_session()
    use(store, old, revision=3)
    correct(store, client)
    use(store, store.create_session(), revision=4)
    [item] = items(store)
    assert (item["answers"], item["chats"]) == (1, 1) and item["sessionIds"] == [old]


def test_a_chat_that_leaves_drops_out_and_the_notice_goes_when_none_is_left(setup):
    store, client, _ = setup
    kept, trashed, gone = store.create_session(), store.create_session(), store.create_session()
    for session in (kept, trashed, gone):
        use(store, session)
    correct(store, client)
    assert items(store)[0]["chats"] == 3
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    [item] = items(store)
    assert item["chats"] == 2 and trashed not in item["sessionIds"]
    archive.trash(store, archive.Trash(sessions=[kept], trashed=True))
    assert [i["chats"] for i in items(store)] == [1]
    archive.trash(store, archive.Trash(sessions=[gone], trashed=True))
    assert items(store) == []
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=False))
    assert items(store)[0]["sessionIds"] == [trashed]


def test_forgetting_a_chat_takes_it_out_of_the_count(tmp_path):
    path = tmp_path / "pi.db"
    remote = Remote()
    transport = httpx.MockTransport(remote.handle)
    with closing(Client("http://memorygate", KEY, "companion", transport=transport)) as client:
        with closing(Store(path)) as store:
            kept, gone = store.create_session(), store.create_session()
            use(store, kept)
            use(store, gone)
            correct(store, client)
            assert items(store)[0]["chats"] == 2
        forget_chat(path, gone, preview_forget(path, gone)["confirmation"])
        with closing(Store(path)) as store:
            [item] = items(store)
            assert (item["answers"], item["chats"], item["sessionIds"]) == (1, 1, [kept])


def test_a_chat_in_the_trash_when_the_change_finishes_gets_its_notice_on_restore(setup):
    store, client, _ = setup
    trashed = store.create_session()
    use(store, trashed)
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    correct(store, client)
    assert items(store) == []
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=False))
    [item] = items(store)
    assert (item["answers"], item["sessionIds"]) == (1, [trashed])
    assert stored(store)[0][0] == "Memory changed"


def test_filing_twice_for_one_request_keeps_one_notice(setup):
    store, _, _ = setup
    use(store, store.create_session())
    with store._connect() as db:
        for _ in range(2):
            recall_notices.file(
                db,
                request_id="same_request_0000001",
                memory_id="mem-1",
                change="corrected",
                revision=3,
            )
    assert len(stored(store)) == 1


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


def test_dismiss_hides_the_notice_once_and_a_bad_id_is_not_found(setup, api):
    store, client, _ = setup
    use(store, store.create_session())
    correct(store, client)
    [item] = items(store)
    path = f"/memory/recall-notices/{item['id']}/dismiss"
    assert owner_allowed("POST", path)
    assert api.post(path).status_code in {401, 403}
    first = api.post(path, headers=ADMIN)
    assert first.status_code == 200 and first.json()["changed"] is True
    assert items(store) == []
    assert api.post(path, headers=ADMIN).json()["changed"] is False
    missing = api.post(f"/memory/recall-notices/mrn_{'0' * 32}/dismiss", headers=ADMIN)
    assert missing.status_code == 404
    other = api.post(f"/memory/recall-notices/plan_{'0' * 32}/dismiss", headers=ADMIN)
    assert other.status_code == 422
