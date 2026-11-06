"""Deleting an imported chat for good (pi.memory.import_deletion).

Its words leave the database (secure delete, the log folded back), a later import of the same
export skips it, a repeat answers with the same record, and the background sender never offers a
word of a chat that is being or has been deleted.
"""

import sqlite3
import threading
from contextlib import closing

import httpx
import pytest
from test_assistant_import_api import KEY, Listener, owner
from test_assistant_import_api import http as http
from test_assistant_import_api import listener as listener
from test_assistant_import_api import offers as offers
from test_assistant_imports import claude_chat, raw

from pi.api import browser_contract
from pi.conversation.store import Store
from pi.memory import import_deletion, import_offers, imports

SECRET = "my bank pin is 4417 and I hide the key under the mat"


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def imported(store, *chats):
    """Import the chats; answers the file and the saved chats' ids by title."""
    chats = list(chats) or [claude_chat("k1", turns=[("human", SECRET), ("assistant", "Ok.")])]
    body = raw(chats)
    imports.save(store, "claude", body, imports.digest(body), memory_configured=True)
    return body, {c["title"]: c["id"] for c in imports.chats(store)["chats"]}


def count(store, table):
    with store._connect() as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_delete_removes_the_chat_its_messages_and_its_offers(store, tmp_path):
    _, ids = imported(
        store, claude_chat("k1", "Private", turns=[("human", SECRET)]), claude_chat("k2", "Other")
    )
    receipt = import_deletion.delete_chat(store, ids["Private"])
    assert receipt["chatId"] == ids["Private"] and receipt["source"] == "claude"
    assert receipt["label"] == "Imported from Claude"
    assert receipt["messagesDeleted"] == 1 and receipt["offeredToMemory"] == 0
    assert receipt["memoryNote"] is None and receipt["fileScrubbed"] is True
    assert count(store, "imported_chats") == 1 and count(store, "imported_messages") == 2
    assert count(store, "import_memory_offers") == 1  # only the other chat's
    with pytest.raises(imports.ImportRefused) as missing:
        imports.chat(store, ids["Private"])
    assert missing.value.status == 404
    # The words are not left in the file's free pages or in the log.
    on_disk = b"".join(path.read_bytes() for path in tmp_path.glob("pi.db*"))
    assert SECRET.encode() not in on_disk


def test_the_record_keeps_ids_and_counts_never_the_title_or_words(store):
    _, ids = imported(store, claude_chat("k1", "Private title", turns=[("human", SECRET)]))
    import_deletion.delete_chat(store, ids["Private title"])
    with closing(sqlite3.connect(store.path)) as db:
        row = db.execute("SELECT * FROM imported_chat_deletions").fetchone()
    assert "Private title" not in repr(row) and SECRET not in repr(row)


def test_a_later_import_of_the_same_export_skips_the_deleted_chat(store):
    body, ids = imported(store)
    import_deletion.delete_chat(store, next(iter(ids.values())))
    preview = imports.preview(store, "claude", body, memory_configured=True)
    assert preview["chats"]["new"] == 0 and preview["chats"]["unchanged"] == 1
    again = imports.save(store, "claude", body, imports.digest(body), memory_configured=True)
    assert again["chats"] == {"new": 0, "updated": 0, "unchanged": 1}
    assert count(store, "imported_chats") == 0 and count(store, "imported_messages") == 0


def test_a_repeat_answers_with_the_same_record_and_an_unknown_chat_is_not_found(store):
    _, ids = imported(store)
    chat_id = next(iter(ids.values()))
    first = import_deletion.delete_chat(store, chat_id)
    second = import_deletion.delete_chat(store, chat_id)
    assert second["fileScrubbed"] is None and {**second, "fileScrubbed": True} == first
    with pytest.raises(imports.ImportRefused) as missing:
        import_deletion.delete_chat(store, "imp_" + "0" * 32)
    assert (missing.value.code, missing.value.status) == ("not_found", 404)


def test_memories_already_made_are_counted_and_named(store):
    _, ids = imported(store)
    with store._connect() as db:
        db.execute("UPDATE import_memory_offers SET state='sent', evidence_id='ev-7'")
    receipt = import_deletion.delete_chat(store, next(iter(ids.values())))
    assert receipt["offeredToMemory"] == 1 and receipt["memoryNote"] == import_deletion.MEMORY_NOTE


def test_the_sender_offers_nothing_of_a_chat_deleted_while_it_waited(store):
    """A row the sender read before the deletion is checked again under the lock and skipped."""
    _, ids = imported(store)
    chat_id = next(iter(ids.values()))
    gate = Listener()
    client = import_offers.ListenerClient(
        "http://memorygate:8020",
        "assistant-imports",
        KEY,
        transport=httpx.MockTransport(gate.handle),
    )
    sender = import_offers.Offers(store, client)

    class Late:
        """The sender's lock, reached only after the chat is deleted."""

        def __init__(self, lock):
            self.lock, self.reached, self.go = lock, threading.Event(), threading.Event()

        def __enter__(self):
            self.reached.set()
            self.go.wait(5)
            return self.lock.__enter__()

        def __exit__(self, *exc):
            return self.lock.__exit__(*exc)

    late = sender.sending = Late(threading.Lock())
    try:
        worker = threading.Thread(target=sender.drain_once)
        worker.start()
        assert late.reached.wait(5)  # the pending row is read, its offer not sent yet
        import_deletion.delete_chat(store, chat_id, sending=late.lock)
        late.go.set()
        worker.join(5)
        assert not worker.is_alive() and gate.requests == []
    finally:
        sender.close()


def test_a_deletion_waits_for_an_offer_being_sent(store):
    _, ids = imported(store)
    lock = threading.Lock()
    with lock:
        deleting = threading.Thread(
            target=import_deletion.delete_chat,
            args=(store, next(iter(ids.values()))),
            kwargs={"sending": lock},
        )
        deleting.start()
        deleting.join(0.3)
        assert deleting.is_alive() and count(store, "imported_chats") == 1
    deleting.join(5)
    assert not deleting.is_alive() and count(store, "imported_chats") == 0


def test_delete_through_the_owner_route(http, listener):
    body = raw([claude_chat("k1", turns=[("human", SECRET)])])
    view = http.post("/imports/claude/preview", content=body, headers=owner()).json()
    http.post(f"/imports/claude/{view['digest']}", content=body, headers=owner())
    chat_id = http.get("/imports/chats", headers=owner()).json()["chats"][0]["id"]
    assert http.post(f"/imports/chats/{chat_id}/delete").status_code in (401, 403)
    done = http.post(f"/imports/chats/{chat_id}/delete", headers=owner())
    assert done.status_code == 200, done.text
    assert done.json()["messagesDeleted"] == 1
    assert http.get(f"/imports/chats/{chat_id}", headers=owner()).status_code == 404
    assert http.get("/imports/chats", headers=owner()).json()["total"] == 0
    missing = http.post("/imports/chats/imp_" + "0" * 32 + "/delete", headers=owner())
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "not_found"
    assert listener.requests == []


def test_the_browser_asks_for_the_password_to_delete():
    path = "/imports/chats/imp_" + "0" * 32 + "/delete"
    assert browser_contract.owner_allowed("POST", path)
    assert not browser_contract.runtime_allowed("POST", path)
    assert browser_contract.password_write("POST", path)
    assert not browser_contract.owner_allowed("GET", path)
