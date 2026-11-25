"""The owner settles a stuck imported-chat erasure by hand (pi.memory.import_settle).

A deleted imported chat's words that an older Pi sent without recording the listener, and that the
listener set now did not find, stay counted as erasing for good (pi.memory.imports.ERASING): no
listener will erase them. The owner may settle one such erasure: Pi clears its own saved copies,
keeps the words hidden for good, records a receipt of counts and closes the Inbox card. It never
claims MemoryGate erased anything. No network: MemoryGate is a stand-in.
"""

import json
import threading
from types import SimpleNamespace

import pytest
from test_assistant_import_api import KEY, owner
from test_assistant_import_api import http as http
from test_assistant_import_api import listener as listener
from test_assistant_import_api import offers as offers
from test_assistant_import_api import store as store
from test_assistant_imports import claude_chat, raw
from test_import_erasure_waiting import OWN, Library, Recalls, configured, package
from test_import_evidence_erase import SECRET, SOURCE, erased, sent_chat

from pi.api import browser_contract
from pi.api import server as api
from pi.context import universal_search
from pi.conversation import hidden_records
from pi.memory import delivery, import_deletion, import_offers, imports
from pi.memory import store as memory_store
from pi.memory.service import Memory
from pi.owner import self_check

DAY = 86400.0
NOT_FOUND = (200, {"status": "not_found", "evidence_id": "ev-1", "memory_ids": []})
# What may hold the words: a listener memory, the evidence and its episode, all titled as imported.
TITLE = "Imported from Claude: Private"
LISTENER = "automatic_listener"


def path(chat_id):
    return f"/imports/erasures/{chat_id}/settle"


def sent(store, sender, export_id, text):
    """Import a chat of its own and send its one word; answers its id."""
    body = raw([claude_chat(export_id, "Private", turns=[("human", text)])])
    imports.save(store, "claude", body, imports.digest(body), memory_configured=True)
    assert sender.drain_once() == 1
    return next(c["id"] for c in imports.chats(store)["chats"] if c["exportId"] == export_id)


def stuck(store, sender, listener):
    """An imported chat an older Pi sent (no listener recorded), deleted; the listener set now
    answers not found, so no listener Pi knows will erase it."""
    chat_id = sent_chat(store, sender, SECRET)
    with store._connect() as db:
        db.execute("UPDATE import_memory_offers SET source_key=NULL")
    import_deletion.delete_chat(store, chat_id, sending=sender.sending, source_key=SOURCE)
    listener.answers = [NOT_FOUND]
    sender.drain_once()
    return chat_id


def saved_copies(store):
    """A finished turn whose saved recall, saved conker.recall result and captured model input
    hold the words (saved before the chat was deleted)."""
    sid = store.create_session()
    tid = store.start_turn(sid)
    memory_store.save_context(store, tid, "ok", package())
    prefix = [{"role": "system", "content": f"{delivery.RECALL_HEADER}\n{SECRET}"}]
    with store._connect() as db:
        db.execute(
            "INSERT INTO recall_results VALUES ('rcl_1',?,?,?,1.0)",
            (sid, tid, json.dumps(package())),
        )
        db.execute(
            "INSERT INTO turn_context_inputs VALUES (?,?,?,NULL,1.0)",
            (tid, "[]", json.dumps(prefix)),
        )
    store.finish_turn(tid, "complete")
    return tid


def held(store):
    with store._connect() as db:
        rows = [
            *db.execute("SELECT package FROM memory_contexts"),
            *db.execute("SELECT package FROM recall_results"),
            *db.execute("SELECT prefix FROM turn_context_inputs"),
        ]
    return " ".join(row[0] or "" for row in rows)


def deleted_at(store):
    with store._connect() as db:
        return db.execute("SELECT deleted_at FROM imported_chat_deletions").fetchone()[0]


def raise_card(store, sender):
    late = deleted_at(store) + 3 * DAY + 1
    check = import_offers.erasure_health(store, sender, now=late)
    assert check["status"] == "degraded" and "cannot tell which memory listener" in check["reason"]
    assert self_check.record(store, {"import_erasures": check}, now=late)[0]["change"] == "raised"
    [card] = self_check.needs_you(store)
    return card


def test_the_owner_settles_a_stuck_erasure_by_hand_and_the_card_closes(
    http, store, offers, listener
):
    chat_id = stuck(store, offers, listener)
    saved_copies(store)
    card = raise_card(store, offers)
    # The card offers the step; the list says which deleted chat it can settle.
    assert card["step"] == {"kind": "settle-import-erasure", "list": "/imports/erasures"}
    assert "settle" in card["reason"]
    listing = http.get("/imports/erasures", headers=owner())
    assert listing.status_code == 200, listing.text
    [item] = listing.json()["erasures"]
    assert (item["deletionId"], item["state"], item["words"]) == (chat_id, "stuck", 1)
    assert item["refusal"] is None

    done = http.post(path(chat_id), headers=owner())

    assert done.status_code == 200, done.text
    receipt = done.json()
    assert receipt["state"] == "settled_by_owner" and receipt["deletionId"] == chat_id
    assert receipt["wordsSettled"] == 1 and receipt["savedRecallCleared"] >= 1
    assert "may remain" in receipt["note"] and "Nothing was erased in MemoryGate" in receipt["note"]
    # Counts and fixed words only: none of the chat's words, its title, ids, keys or listener.
    text = json.dumps(receipt)
    for trace in (SECRET, "Private", "ev-1", SOURCE, KEY, "bank"):
        assert trace not in text
    # Pi's own saved copies are gone: saved recall, saved conker.recall results, captured input.
    assert SECRET not in held(store)
    # The card closes at once, and stays closed.
    assert self_check.needs_you(store) == []
    assert import_offers.erasure_health(store, offers, now=deleted_at(store) + 9 * DAY) == {
        "status": "ok",
        "erasing": 0,
    }
    history = http.get("/imports", headers=owner()).json()["memory"]
    assert history["settledByHand"] == 1 and history["notFoundToErase"] == 0
    assert history["problem"] is None
    [item] = http.get("/imports/erasures", headers=owner()).json()["erasures"]
    assert (item["state"], item["settledAt"]) == ("settled_by_owner", receipt["settledAt"])


def test_nothing_of_the_import_comes_back_after_settling(http, store, offers, listener):
    chat_id = stuck(store, offers, listener)
    assert http.post(path(chat_id), headers=owner()).status_code == 200
    # A turn's recall, which is what the model reads, and a conker.recall search.
    tid = store.start_turn(store.create_session())
    memory = Memory(store, Recalls(package()))
    memory.prepare(tid, "What do you know about me?")
    saved = memory_store.context(store, tid)["package"]
    assert saved["memories"] == [OWN] and saved["episodes"] == [] and saved["evidence"] == []
    found, state = memory.search(tid, "bank", 5)
    assert state == "ok" and found["memories"] == [OWN] and SECRET not in json.dumps(found)
    # Owner search: the listener memory, the evidence, its episode and what was made from it.
    cards = [
        {
            "type": "memory",
            "id": "m-imp",
            "title": "needle",
            "preview": SECRET,
            "source_type": LISTENER,
        },
        {"type": "episode", "id": "ep-1", "title": TITLE + " needle", "preview": SECRET},
        {"type": "evidence", "id": "ev-1", "title": "needle", "preview": SECRET},
        {"type": "analysis", "id": "an-1", "title": "needle", "preview": SECRET},
        {"type": "observation", "id": "ob-1", "title": "needle", "preview": SECRET},
        {
            "type": "memory",
            "id": "m-own",
            "title": "needle",
            "preview": "tea",
            "source_type": "user",
        },
    ]
    rows, _ = universal_search.lexical(
        store,
        SimpleNamespace(client=Library(cards)),
        None,
        "needle",
        {"sources": ["memory"]},
        "metadata",
    )
    assert {row["recordId"] for row in rows} == {"m-own"}
    # The memory explorer and every other reader still name the evidence by id.
    with store._connect() as db:
        assert "ev-1" in hidden_records.hidden_records(db)["evidence"]
        assert hidden_records.erasing(db)


@pytest.mark.parametrize("cause", ["waiting", "another listener set now"])
def test_settling_is_refused_while_a_listener_set_now_can_still_erase_it(
    http, store, offers, listener, monkeypatch, cause
):
    if cause == "waiting":
        chat_id = sent_chat(store, offers, SECRET)
        import_deletion.delete_chat(store, chat_id, sending=offers.sending, source_key=SOURCE)
        listener.answers = [(503, {"detail": "down"})]
        offers.drain_once()
    else:  # not found where Pi looked; a listener configured since then is asked once
        chat_id = stuck(store, offers, listener)
        other = configured(offers, listener, "old-imports")
        monkeypatch.setattr(api.app.state, "import_offers", other, raising=False)
    saved_copies(store)
    refused = http.post(path(chat_id), headers=owner())
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "listener_reachable" and "Wait" in detail["message"]
    assert SECRET not in refused.text and SOURCE not in refused.text
    # Nothing changed: no receipt, saved copies kept, still counted as erasing.
    assert SECRET in held(store)
    [item] = http.get("/imports/erasures", headers=owner()).json()["erasures"]
    assert item["state"] == "erasing" and item["refusal"]["code"] == "listener_reachable"
    with store._connect() as db:
        open_rows = f"SELECT COUNT(*) FROM import_evidence_erasures WHERE {imports.OPEN}"
        assert db.execute(open_rows).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM import_erasure_settlements").fetchone()[0] == 0
    if cause != "waiting":
        other.close()


def test_settling_is_refused_when_the_erasure_is_not_stuck(http, store, offers, listener):
    # Recorded under another listener: setting it back erases them.
    elsewhere = sent_chat(store, offers, SECRET)
    with store._connect() as db:
        db.execute("UPDATE import_memory_offers SET source_key='old-imports'")
    import_deletion.delete_chat(store, elsewhere, sending=offers.sending, source_key=SOURCE)
    # Erased in MemoryGate: nothing is left to settle.
    done = sent(store, offers, "k2", "I prefer tea in the morning")
    import_deletion.delete_chat(store, done, sending=offers.sending, source_key=SOURCE)
    listener.answers = [erased("ev-2")]
    assert offers.drain_once() == 1
    for chat_id in (elsewhere, done):
        refused = http.post(path(chat_id), headers=owner())
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "not_stuck"
    unknown = http.post(path("imp_" + "0" * 32), headers=owner())
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "not_found"
    assert http.post(path("imp_x"), headers=owner()).status_code == 403  # not a route
    assert http.post(path(elsewhere)).status_code in (401, 403)
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM import_erasure_settlements").fetchone()[0] == 0


def test_with_no_listener_set_up_a_waiting_erasure_is_not_stuck(http, store, offers, monkeypatch):
    chat_id = sent_chat(store, offers, SECRET)
    import_deletion.delete_chat(store, chat_id, sending=offers.sending, source_key=None)
    monkeypatch.setattr(api.app.state, "import_offers", import_offers.Offers(store, None))
    refused = http.post(path(chat_id), headers=owner())
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "not_stuck"
    # Without the sender Pi cannot tell what is configured: it refuses.
    monkeypatch.setattr(api.app.state, "import_offers", None)
    assert http.post(path(chat_id), headers=owner()).status_code == 503


def test_settling_again_answers_with_the_same_receipt(http, store, offers, listener):
    chat_id = stuck(store, offers, listener)
    first = http.post(path(chat_id), headers=owner()).json()
    generation = memory_store.generation(store)
    saved_copies(store)  # saved after the settlement: a repeat clears nothing
    again = http.post(path(chat_id), headers=owner())
    assert again.status_code == 200
    assert again.json() == {**first, "fileScrubbed": None}
    assert memory_store.generation(store) == generation and SECRET in held(store)


def test_two_settlements_at_once_settle_it_once(store, offers, listener):
    from pi.memory import import_settle

    chat_id = stuck(store, offers, listener)
    replies, start = [], threading.Barrier(2)

    def settle():
        start.wait()
        replies.append(import_settle.settle(store, chat_id, listener=SOURCE))

    threads = [threading.Thread(target=settle) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(replies) == 2 and len({reply["settledAt"] for reply in replies}) == 1
    # One settled it; the other found it settled and answered with the same record.
    assert sorted(reply["fileScrubbed"] is None for reply in replies) == [False, True]
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM import_erasure_settlements").fetchone()[0] == 1


def test_a_listener_that_still_finds_the_words_later_erases_them(store, offers, listener):
    # Settled by hand, then the owner sets up the listener that does hold them: it erases them,
    # and what the import hid comes back.
    chat_id = stuck(store, offers, listener)
    from pi.memory import import_settle

    receipt = import_settle.settle(store, chat_id, listener=SOURCE)
    other = configured(offers, listener, "old-imports")
    listener.answers = [erased("ev-1")]
    assert other.drain_once() == 1
    with store._connect() as db:
        assert not hidden_records.erasing(db)
    assert import_settle.settle(store, chat_id, listener="old-imports") == {
        **receipt,
        "fileScrubbed": None,
    }
    other.close()


def test_an_older_pi_counts_a_settled_erasure_as_erasing_again(store, offers, listener):
    # Rollback: an older Pi does not know the settlements table; the rows it reads are unchanged
    # and still erasing (imports.ERASING), so the words stay hidden and its card comes back.
    chat_id = stuck(store, offers, listener)
    from pi.memory import import_settle

    import_settle.settle(store, chat_id, listener=SOURCE)
    with store._connect() as db:
        rows = db.execute(
            f"SELECT evidence_id FROM import_evidence_erasures WHERE {imports.ERASING}"
        ).fetchall()
    assert [row[0] for row in rows] == ["ev-1"]


def test_the_browser_asks_for_the_password_to_settle():
    route = path("imp_" + "0" * 32)
    assert browser_contract.owner_allowed("POST", route)
    assert not browser_contract.runtime_allowed("POST", route)
    assert browser_contract.password_write("POST", route)
    assert browser_contract.owner_allowed("GET", "/imports/erasures")
    assert not browser_contract.password_write("GET", "/imports/erasures")
    for lookalike in (path("imp_x"), path("imp_" + "0" * 32) + "/x", "/imports/erasures/x"):
        assert not browser_contract.owner_allowed("POST", lookalike), lookalike
