"""While a deleted imported chat's erasure in MemoryGate waits, its words stay out of everything Pi
reads back (pi.conversation.hidden_records), and an erasure that does not settle is reported in
the Inbox (pi.memory.import_offers.erasure_health, pi.owner.self_check). No network."""

import json

import pytest
from test_import_evidence_erase import SECRET, SOURCE, erased, sent_chat, setup  # noqa: F401

from pi.context import controls as context_controls
from pi.conversation import settings as session_settings
from pi.memory import import_deletion, import_offers
from pi.memory import store as memory_store
from pi.memory.service import Memory
from pi.owner import self_check
from pi.turns import context as turn_context
from pi.turns import recall

TITLE = "Imported from Claude: Private"
IMPORTED = {"id": "m-imp", "text": SECRET, "source_type": "automatic_listener"}
OWN = {"id": "m-own", "text": "I prefer tea in the morning", "source_type": "user"}
EVIDENCE = {"id": "ev-1", "title": TITLE, "summary": SECRET, "source": SOURCE}
EPISODE = {"id": "ep-1", "title": TITLE, "summary": SECRET}
DAY = 86400.0


def package(*memories):
    return {
        "memories": list(memories or (IMPORTED, OWN)),
        "episodes": [EPISODE],
        "evidence": [EVIDENCE],
        "retrieval": {"semantic": {"status": "ok"}},
    }


class Recalls:
    """MemoryGate's recall: it still holds the deleted chat's words while the erasure waits."""

    agent_id = "companion"

    def __init__(self, found):
        self.found = found

    def retrieve(self, query, **options):
        return json.loads(json.dumps(self.found))


def deleted(store, sender):
    """An imported chat whose words went to MemoryGate, deleted; its erasure waits."""
    chat_id = sent_chat(store, sender, SECRET)
    import_deletion.delete_chat(store, chat_id, sending=sender.sending, source_key=SOURCE)
    return chat_id


def kept(store):
    with store._connect() as db:
        rows = [
            *db.execute("SELECT package FROM memory_contexts"),
            *db.execute("SELECT package FROM recall_results"),
        ]
    return " ".join(row[0] or "" for row in rows)


def test_turn_recall_and_a_search_leave_out_a_deleted_import_while_its_erasure_waits(setup):
    store, sender, *_ = setup
    deleted(store, sender)
    sid = store.create_session()
    tid = store.start_turn(sid)
    memory = Memory(store, Recalls(package()))
    memory.prepare(tid, "What do you know about me?")
    saved = memory_store.context(store, tid)["package"]
    assert saved["memories"] == [OWN] and SECRET not in json.dumps(saved)
    found, state = memory.search(tid, "bank", 5)
    assert state == "ok" and found["memories"] == [OWN] and SECRET not in json.dumps(found)


def test_saved_recall_the_chat_view_and_a_retry_hide_it_while_the_erasure_waits(setup):
    store, sender, *_ = setup
    sid = store.create_session()
    tid = store.start_turn(sid)
    store.append_message(sid, "user", "What do you know?", turn_id=tid, purpose="input")
    memory_store.save_context(store, tid, "ok", package())
    with store._connect() as db:
        db.execute(
            "INSERT INTO recall_results VALUES ('rcl_1',?,?,?,1.0)",
            (sid, tid, json.dumps(package())),
        )
    store.finish_turn(tid, "complete")
    deleted(store, sender)
    # The chat view (GET /sessions/{id}).
    view = memory_store.statuses(store, sid, [tid], configured=True)
    assert SECRET not in json.dumps(view)
    # A parked turn's saved recall, read again on use.
    selected = session_settings.execution(store, sid, tid)
    assert SECRET not in json.dumps(Memory(store, Recalls({})).saved_context(tid, selected))
    # A saved conker.recall result in a later history.
    row = {"session_id": sid, "package": json.dumps(package())}
    assert SECRET not in json.dumps(recall._still_shown(store, row))
    # A retry would send the saved input, recall text and all: refused.
    with pytest.raises(context_controls.ContextError) as refused:
        turn_context._require_visible_recall(store, tid, sid)
    assert refused.value.detail["code"] == "retry_source_unavailable"


def test_a_recall_in_flight_across_the_deletion_is_not_kept(setup):
    store, sender, *_ = setup
    tid = store.start_turn(store.create_session())

    class Late:
        """Ranks after the hidden records were left out; the chat is deleted meanwhile."""

        def rank_for_turn(self, query, found, selected):
            deleted(store, sender)
            return found

    Memory(store, Recalls(package()), ranker=Late()).prepare(tid, "Anything?")
    saved = memory_store.context(store, tid)
    assert (saved["status"], saved["package"]) == ("unavailable", None)
    assert SECRET not in kept(store)


def test_deleting_a_sent_chat_advances_the_recall_generation(setup):
    store, sender, *_ = setup
    chat_id = sent_chat(store, sender, SECRET)
    before = memory_store.generation(store)
    import_deletion.delete_chat(store, chat_id, sending=sender.sending, source_key=SOURCE)
    assert memory_store.generation(store) == before + 1
    # A repeated request queues nothing new and changes nothing.
    import_deletion.delete_chat(store, chat_id, sending=sender.sending, source_key=SOURCE)
    assert memory_store.generation(store) == before + 1


def test_listener_memories_come_back_once_the_erasure_settles(setup):
    store, sender, listener, _ = setup
    deleted(store, sender)
    other = {
        "id": "m-other",
        "text": "an imported chat still kept",
        "source_type": IMPORTED["source_type"],
    }
    found = {**package(other, OWN), "episodes": [], "evidence": []}
    first = store.start_turn(store.create_session())
    Memory(store, Recalls(found)).prepare(first, "Anything?")
    assert memory_store.context(store, first)["package"]["memories"] == [OWN]
    listener.answers = [erased("ev-1")]
    assert sender.drain_once() == 1
    later = store.start_turn(store.create_session())
    Memory(store, Recalls(found)).prepare(later, "Anything?")
    assert memory_store.context(store, later)["package"]["memories"] == [other, OWN]


def health(store, sender, now):
    return import_offers.erasure_health(store, sender, now=now)


def test_an_erasure_waiting_three_days_files_one_inbox_item_until_it_settles(setup):
    store, sender, listener, clock = setup
    deleted(store, sender)
    with store._connect() as db:
        deleted_at = db.execute("SELECT deleted_at FROM imported_chat_deletions").fetchone()[0]
    listener.answers = [(503, {"detail": "down"})]
    sender.drain_once()
    soon = health(store, sender, deleted_at + DAY)
    assert soon["status"] == "ok" and soon["erasing"] == 1
    late = deleted_at + 3 * DAY + 1
    stuck = health(store, sender, late)
    assert stuck["status"] == "degraded" and "still stored in memory" in stuck["reason"]
    assert self_check.record(store, {"import_erasures": stuck}, now=late)[0]["change"] == "raised"
    [item] = self_check.needs_you(store)
    assert item["kind"] == "health" and "still stored in memory" in item["reason"]
    assert self_check.record(store, {"import_erasures": stuck}, now=late + 300) == []
    clock.now += 3600
    listener.answers = [erased("ev-1")]
    assert sender.drain_once() == 1
    assert health(store, sender, late + 600)["status"] == "ok"
    changes = self_check.record(
        store, {"import_erasures": health(store, sender, late + 600)}, now=late + 600
    )
    assert [change["change"] for change in changes] == ["cleared"]
    assert self_check.needs_you(store) == []


@pytest.mark.parametrize(
    ("code", "fix"),
    [(401, "PI_MEMORYGATE_IMPORT_KEY"), (403, "PI_MEMORYGATE_IMPORT_KEY"), (404, "SOURCE")],
)
def test_a_refused_listener_files_the_inbox_item_at_once(setup, code, fix):
    store, sender, listener, clock = setup
    deleted(store, sender)
    listener.answers = [(code, {"detail": "refused"})]
    sender.drain_once()
    now = clock.now
    check = health(store, sender, now)
    assert check["status"] == "degraded" and fix in check["reason"]
    assert len(check["reason"]) <= self_check.REASON_CHARS
    assert "listener-key" not in json.dumps(check) and SECRET not in json.dumps(check)
    [raised] = self_check.record(store, {"import_erasures": check}, now=now)
    assert raised["change"] == "raised"
    [item] = self_check.needs_you(store)
    assert item["title"] == "Erasing deleted imported chats is not working"
    clock.now += import_offers.PAUSE_SECONDS + 1
    listener.answers = [erased("ev-1")]
    assert sender.drain_once() == 1
    self_check.record(store, {"import_erasures": health(store, sender, clock.now)}, now=clock.now)
    assert self_check.needs_you(store) == []


def test_an_erasure_for_another_listener_names_the_setting_after_three_days(setup):
    store, sender, *_ = setup
    chat_id = sent_chat(store, sender, SECRET)
    import_deletion.delete_chat(store, chat_id, sending=sender.sending, source_key="old-imports")
    sender.drain_once()
    with store._connect() as db:
        deleted_at = db.execute("SELECT deleted_at FROM imported_chat_deletions").fetchone()[0]
    assert health(store, sender, deleted_at + DAY)["status"] == "ok"
    check = health(store, sender, deleted_at + 3 * DAY + 1)
    assert check["status"] == "degraded" and "PI_MEMORYGATE_IMPORT_SOURCE back" in check["reason"]
    assert "old-imports" not in json.dumps(check)
