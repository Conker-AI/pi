"""Opening a chat shows each turn's saved recall without the records of another chat in the Trash
(pi.conversation.hidden_records, the rule recall itself uses); restoring that chat brings them back,
and a chat opened by its own id still shows its own. No network."""

from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient
from test_visibility_memory import AGENT, delivered, trash

from pi.api import server as api
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory.hidden import _receipt_ids
from pi.memory.service import Memory, MemoryClient
from pi.models.routing import Router
from pi.turns.loop import Loop

KEY = "test-admin"
TRASHED = "the spare key is under the blue pot"


@pytest.fixture
def http(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        memory = Memory(
            store,
            MemoryClient(
                "http://memory.test",
                "ingest-secret-long",
                "read-secret-long",
                AGENT,
                transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            ),
        )
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(
            api.app.state, "loop", Loop(store, Router(), memory=memory), raising=False
        )
        monkeypatch.setattr(api.app.state, "admin_key", KEY, raising=False)
        yield store, TestClient(api.app)
        memory.close()


def opened(http, session):
    response = http.get(f"/sessions/{session}", headers={"X-Pi-Key": KEY})
    assert response.status_code == 200
    return response.text


def test_a_chat_does_not_show_recall_from_another_chat_in_the_trash(http):
    store, client = http
    chat, other = store.create_session(), store.create_session()
    _, (_, _, memory) = delivered(store, other, TRASHED)
    tid = store.start_turn(chat)
    recalled = {"memories": [{"id": memory, "text": TRASHED}], "retrieval": {}}
    memory_store.save_context(store, tid, "ok", recalled)
    store.finish_turn(tid, "complete")
    assert TRASHED in opened(client, chat)
    trash(store, [other])
    assert TRASHED not in opened(client, chat)
    trash(store, [other], trashed=False)
    assert TRASHED in opened(client, chat)


def test_a_trashed_chat_opened_by_its_own_id_still_shows_its_own_recall(http):
    store, client = http
    chat = store.create_session()
    _, (_, _, memory) = delivered(store, chat, TRASHED)
    tid = store.start_turn(chat)
    recalled = {"memories": [{"id": memory, "text": TRASHED}], "retrieval": {}}
    memory_store.save_context(store, tid, "ok", recalled)
    store.finish_turn(tid, "complete")
    trash(store, [chat])
    assert TRASHED in opened(client, chat)


def test_a_trashed_chat_sent_before_destinations_were_recorded_is_still_hidden(http):
    # An old database: the message's MemoryGate namespace was never recorded and Pi has no other,
    # so only the configured reading agent names the memory made from it.
    store, client = http
    chat, other = store.create_session(), store.create_session()
    message = store.append_message(other, "user", TRASHED)["id"]
    memory = _receipt_ids(AGENT, message)[2]
    tid = store.start_turn(chat)
    recalled = {"memories": [{"id": memory, "text": TRASHED}], "retrieval": {}}
    memory_store.save_context(store, tid, "ok", recalled)
    store.finish_turn(tid, "complete")
    trash(store, [other])
    assert TRASHED not in opened(client, chat)


def test_opening_a_chat_reads_saved_recall_on_one_connection_however_many_turns(http):
    store, _ = http
    chat = store.create_session()
    for index in range(30):
        tid = store.start_turn(chat)
        recalled = {"memories": [{"id": f"mem-{index}", "text": "a fact"}], "retrieval": {}}
        memory_store.save_context(store, tid, "ok", recalled)
        store.finish_turn(tid, "complete")
    turns = [turn["id"] for turn in store.turns(chat)]
    opened_connections = []
    original = store._connect

    def counting():
        opened_connections.append(1)
        return original()

    store._connect = counting
    try:
        few = memory_store.statuses(store, chat, turns[:2], configured=True, agents=(AGENT,))
        two = len(opened_connections)
        opened_connections.clear()
        many = memory_store.statuses(store, chat, turns, configured=True, agents=(AGENT,))
    finally:
        del store._connect
    assert len(opened_connections) == two
    assert len(many["turns"]) == 30 and few["turns"][turns[0]] == many["turns"][turns[0]]
