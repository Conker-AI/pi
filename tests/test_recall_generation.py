"""Erasing a deleted imported chat's words in MemoryGate clears Pi's saved recall, as forgetting a
memory does, and a recall in flight across that erasure is never saved (the recall generation in
pi.memory.store). Recall is not paused while erasures wait. No network."""

import json

import httpx
import pytest
from memory_edits_helpers import BODY as CORRECTION
from memory_edits_helpers import KEY as CORRECTION_KEY
from memory_edits_helpers import Remote
from test_agent_recall import recall_call
from test_import_evidence_erase import SECRET, SOURCE, erased, sent_chat, setup  # noqa: F401
from test_memory_forget import BODY, client, forgotten
from test_tool_turns import FakeGate, build

from pi.context import controls as context_controls
from pi.memory import edits as memory_edits
from pi.memory import forget as memory_forget
from pi.memory import import_deletion
from pi.memory import store as memory_store
from pi.memory.corrections import Client
from pi.memory.service import Memory
from pi.models.providers import Message
from pi.models.routing import Router
from pi.turns import context as turn_context
from pi.turns.loop import Loop

ERASED = {"id": "m-1", "text": SECRET}


def package(*memories):
    return {"memories": list(memories), "retrieval": {"semantic": {"status": "ok"}}}


def saved_anywhere(store):
    """Every place Pi keeps recalled words for later: turn packages, conker.recall results and
    captured answer inputs."""
    with store._connect() as db:
        rows = [
            *db.execute("SELECT package FROM memory_contexts"),
            *db.execute("SELECT package FROM recall_results"),
            *db.execute("SELECT prefix FROM turn_context_inputs"),
        ]
    return " ".join(row[0] or "" for row in rows)


class Gone:
    """The import listener: MemoryGate erased the event and the memory it alone supported."""

    source_key = SOURCE

    def erase(self, evidence_id):
        return True


def erasure_settles(store):
    """A deleted imported chat's queued erasure goes through now (the background sender)."""
    with store._connect() as db:
        db.execute(
            "INSERT INTO import_evidence_erasures(evidence_id, chat_id, source_key) "
            "VALUES ('ev-9', 'imp_x', ?)",
            (SOURCE,),
        )
    assert import_deletion.erase_waiting(store, Gone()).done == 1


def test_erasing_an_imported_chat_clears_every_saved_recall(setup):
    store, sender, listener, _ = setup
    chat_id = sent_chat(store, sender, SECRET)
    sid = store.create_session()
    tid = store.start_turn(sid)
    memory_store.save_context(store, tid, "ok", package(ERASED))
    turn_context.capture(store, tid, [], [Message("system", turn_context.RECALL_HEADER + SECRET)])
    with store._connect() as db:
        db.execute(
            "INSERT INTO recall_results VALUES ('rcl_1',?,?,?,1.0)",
            (sid, tid, json.dumps({"memories": [ERASED]})),
        )
    store.finish_turn(tid, "complete")
    import_deletion.delete_chat(store, chat_id, sending=sender.sending, source_key=SOURCE)
    # Recall goes on while the erasure waits: MemoryGate still holds the words until it answers.
    assert memory_store.recall_held(store) == 0
    listener.answers = [erased("ev-1")]
    assert sender.drain_once() == 1
    assert SECRET not in saved_anywhere(store)
    assert memory_store.context(store, tid)["status"] == "redacted"


def test_an_erasure_whose_answer_was_lost_still_clears_saved_recall(setup):
    store, sender, listener, clock = setup
    chat_id = sent_chat(store, sender, SECRET)
    tid = store.start_turn(store.create_session())
    memory_store.save_context(store, tid, "ok", package(ERASED))
    store.finish_turn(tid, "complete")
    import_deletion.delete_chat(store, chat_id, sending=sender.sending, source_key=SOURCE)
    listener.answers = [(503, {})]  # MemoryGate erased it; the answer never reached Pi
    sender.drain_once()
    clock.now += 60
    listener.answers = [(200, {"status": "not_found", "evidence_id": "ev-1"})]
    sender.drain_once()
    assert SECRET not in saved_anywhere(store)


class Racing:
    """MemoryGate's answer was made before the erasure, which settles while it is in flight."""

    agent_id = "companion"

    def __init__(self, store, answers):
        self.store, self.answers, self.held = store, list(answers), []

    def retrieve(self, query, **options):
        found, race = self.answers.pop(0)
        if race:
            erasure_settles(self.store)
            self.held.append(memory_store.recall_held(self.store))
        return package(*found)


def test_a_turn_recall_that_straddles_an_erasure_is_not_saved(setup):
    store = setup[0]
    tid = store.start_turn(store.create_session())
    racing = Racing(store, [([ERASED], True)])
    Memory(store, racing).prepare(tid, "What do you remember?")
    assert racing.held == [0]
    saved = memory_store.context(store, tid)
    assert (saved["status"], saved["package"]) == ("unavailable", None)
    assert SECRET not in saved_anywhere(store)
    # The next recall reads after the erasure and is saved as usual.
    later = store.start_turn(store.create_session())
    Memory(store, Racing(store, [([], False)])).prepare(later, "And now?")
    assert memory_store.context(store, later)["status"] == "ok"


def test_a_conker_recall_search_that_straddles_an_erasure_is_not_saved(setup):
    store = setup[0]
    loop, provider = build(store, [recall_call(), "Nothing to add."], FakeGate())
    loop.memory = Memory(store, Racing(store, [([], False), ([ERASED], True)]))
    loop.run_turn(store.create_session(), "When is Dana's birthday?")
    assert SECRET not in saved_anywhere(store)
    seen = "\n".join(m.content for m in provider.sent[-1] if m.role == "tool")
    assert SECRET not in seen and "unavailable" in seen


def test_an_answer_input_captured_across_an_erasure_is_refused(setup, monkeypatch):
    store = setup[0]
    sid = store.create_session()
    tid = store.start_turn(sid)
    store.append_message(sid, "user", "An ordinary request", turn_id=tid, purpose="input")
    memory_store.save_context(store, tid, "ok", package(ERASED))
    original = turn_context.capture

    def late(*args, **kwargs):
        erasure_settles(store)  # after the history read the saved recall, before it is kept
        return original(*args, **kwargs)

    monkeypatch.setattr(turn_context, "capture", late)
    with pytest.raises(context_controls.ContextError) as refused:
        Loop(store, Router())._history(sid, turn_id=tid)
    assert refused.value.detail["code"] == "context_unavailable"
    assert SECRET not in saved_anywhere(store)


def test_forget_and_correction_advance_the_generation_like_an_erasure(setup):
    store = setup[0]
    with store._connect() as db:
        start = memory_store.generation_in(db)
    erasure_settles(store)
    # Each clears saved recall twice: when it starts and when MemoryGate confirms it.
    memory_forget.forget(store, client(forgotten), BODY)
    remote = Remote()
    corrections = Client(
        "http://memorygate",
        CORRECTION_KEY,
        "companion",
        transport=httpx.MockTransport(remote.handle),
    )
    assert memory_edits.apply(store, corrections, CORRECTION)["status"] == "applied"
    with store._connect() as db:
        assert memory_store.generation_in(db) == start + 5
