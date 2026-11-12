"""Deleting an imported chat erases its words in MemoryGate too (pi.memory.import_deletion).

The deletion queues the evidence ids of every word that went to MemoryGate; the background
sender erases them through the same listener, before it offers anything new, and keeps trying
until MemoryGate answers. MemoryGate is a stand-in that records each request.
"""

from contextlib import closing

import httpx
import pytest
from test_assistant_import_api import KEY, Listener
from test_assistant_imports import claude_chat, raw

from pi.conversation.store import Store
from pi.memory import import_deletion, import_offers, imports

SECRET = "my bank pin is 4417 and I hide the key under the mat"


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def setup(tmp_path):
    listener, clock = Listener(), Clock()
    client = import_offers.ListenerClient(
        "http://memorygate:8020",
        "assistant-imports",
        KEY,
        transport=httpx.MockTransport(listener.handle),
    )
    with closing(Store(tmp_path / "pi.db")) as store:
        sender = import_offers.Offers(store, client, clock=clock)
        yield store, sender, listener, clock
        sender.close()


def sent_chat(store, sender, *texts):
    """Import one chat and send its words; answers the chat id."""
    body = raw([claude_chat("k1", "Private", turns=[("human", text) for text in texts])])
    imports.save(store, "claude", body, imports.digest(body), memory_configured=True)
    assert sender.drain_once() == len(texts)
    return imports.chats(store)["chats"][0]["id"]


def waiting(store):
    with store._connect() as db:
        return [
            tuple(row)
            for row in db.execute(
                "SELECT evidence_id, state, attempts FROM import_evidence_erasures "
                "ORDER BY evidence_id"
            )
        ]


def erased(evidence_id):
    return 200, {"status": "erased", "evidence_id": evidence_id, "memory_ids": ["m-1"]}


def test_deleting_a_sent_chat_erases_each_word_in_memorygate(setup):
    store, sender, listener, _ = setup
    chat_id = sent_chat(store, sender, SECRET, "I prefer tea in the morning")
    assert waiting(store) == []

    receipt = import_deletion.delete_chat(store, chat_id, sending=sender.sending)

    assert receipt["offeredToMemory"] == 2 and receipt["memoryNote"] == import_deletion.MEMORY_NOTE
    assert waiting(store) == [("ev-1", "pending", 0), ("ev-2", "pending", 0)]
    assert import_offers.status(store, sender)["erasing"] == 2
    listener.answers = [erased("ev-1"), erased("ev-2")]
    assert sender.drain_once() == 2

    erases = listener.requests[2:]
    assert [(r.method, r.url.path) for r in erases] == [
        ("DELETE", "/runtime/listeners/assistant-imports/evidence/ev-1"),
        ("DELETE", "/runtime/listeners/assistant-imports/evidence/ev-2"),
    ]
    assert all(r.headers["X-MemoryGate-Listener-Key"] == KEY for r in erases)
    assert waiting(store) == []
    status = import_offers.status(store, sender)
    assert (status["erasing"], status["notFoundToErase"], status["problem"]) == (0, 0, None)


def test_a_failed_erase_is_retried_later_and_a_refused_listener_pauses(setup):
    store, sender, listener, clock = setup
    import_deletion.delete_chat(store, sent_chat(store, sender, SECRET), sending=sender.sending)

    listener.answers = [(503, {"detail": "down"})]
    sender.drain_once()
    assert waiting(store) == [("ev-1", "pending", 1)]
    assert "retried" in import_offers.status(store, sender)["problem"]
    requests = len(listener.requests)
    sender.drain_once()  # not due yet
    assert len(listener.requests) == requests

    clock.now += 10
    listener.answers = [(404, {"detail": "Evidence source not found"})]
    sender.drain_once()
    assert waiting(store) == [("ev-1", "pending", 1)]  # a refused listener costs no attempt
    assert "HTTP 404" in import_offers.status(store, sender)["problem"]
    clock.now += import_offers.PAUSE_SECONDS + 1
    listener.answers = [erased("ev-1")]
    assert sender.drain_once() == 1 and waiting(store) == []
    assert import_offers.status(store, sender)["problem"] is None


def test_an_event_memorygate_does_not_have_is_counted_and_not_retried(setup):
    store, sender, listener, clock = setup
    import_deletion.delete_chat(store, sent_chat(store, sender, SECRET), sending=sender.sending)
    listener.answers = [(200, {"status": "not_found", "evidence_id": "ev-1", "memory_ids": []})]

    sender.drain_once()
    clock.now += 1_000
    requests = len(listener.requests)
    sender.drain_once()

    assert waiting(store) == [("ev-1", "missing", 0)]
    assert len(listener.requests) == requests
    assert import_offers.status(store, sender)["notFoundToErase"] == 1


def test_a_wrong_acknowledgement_is_not_taken_as_erased(setup):
    store, sender, listener, _ = setup
    import_deletion.delete_chat(store, sent_chat(store, sender, SECRET), sending=sender.sending)
    listener.answers = [erased("ev-other")]

    sender.drain_once()

    assert waiting(store) == [("ev-1", "pending", 1)]


def test_erasing_comes_before_new_offers_and_an_odd_id_stays_inside_the_path(setup):
    store, sender, listener, _ = setup
    chat_id = sent_chat(store, sender, SECRET)
    with store._connect() as db:
        db.execute("UPDATE import_memory_offers SET evidence_id='../../memory/x'")
    import_deletion.delete_chat(store, chat_id, sending=sender.sending)
    body = raw([claude_chat("k2", "Next", turns=[("human", "I want a new desk")])])
    imports.save(store, "claude", body, imports.digest(body), memory_configured=True)
    listener.answers = [erased("../../memory/x")]

    assert sender.drain_once() == 2

    erase, offer = listener.requests[1:]
    assert erase.method == "DELETE"
    assert (
        erase.url.raw_path == b"/runtime/listeners/assistant-imports/evidence/..%2F..%2Fmemory%2Fx"
    )
    assert offer.method == "POST"


def test_without_a_listener_the_erasures_wait_and_say_why(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        with store._connect() as db:
            db.execute(
                "INSERT INTO import_evidence_erasures(evidence_id, chat_id) VALUES ('ev-9', 'imp_x')"
            )
        status = import_offers.status(store, import_offers.Offers(store, None))
    assert status["erasing"] == 1 and "not set up" in status["problem"]
