"""Memory edits: the hold on recall, active turns, concurrency and the acknowledgement generation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_memory_forget_recall import recorded, retry_copy, unavailable
from waiting import WAIT, wait_event

from pi.context import controls as context_controls
from pi.memory import delivery as memory_delivery
from pi.memory import edits
from pi.memory import store as memory_store
from pi.models.providers import Message
from pi.turns import context as turn_context
from tests.memory_edits_helpers import (
    BODY,
    setup,  # noqa: F401 -- shared fixture
    unresolved,
)


def test_active_turn_preparation_and_thinking_refuse_before_remote_mutation(setup):
    store, client, remote = setup
    sid = store.create_session()
    tid = store.start_turn(sid)
    with pytest.raises(edits.EditError) as error:
        edits.apply(store, client, BODY)
    assert error.value.detail["code"] == "work_active"
    store.finish_turn(tid, "complete")
    with store._connect() as db:
        db.execute(
            "INSERT INTO background_thinking(turn_id,session_id,status,created_at) VALUES(?,?,'queued',0)",
            (tid, sid),
        )
    with pytest.raises(edits.EditError) as error:
        edits.apply(store, client, BODY)
    assert error.value.detail["code"] == "work_active"
    assert remote.applied == 0 and edits.pending(store)["pending"] is None


def test_one_admitted_edit_and_no_concurrent_duplicate_dispatch(setup):
    store, client, remote = setup
    entered, release = Event(), Event()
    original = client.apply

    def blocked(*args):
        entered.set()
        wait_event(release, "the test to release the held call")
        return original(*args)

    client.apply = blocked
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(edits.apply, store, client, BODY)
        wait_event(entered, "the held call to start")
        try:
            assert memory_store.recall_held(store)
            with pytest.raises(edits.EditError) as error:
                edits.apply(store, client, BODY)
            assert error.value.detail["code"] == "in_progress"
        finally:
            release.set()
        assert future.result(WAIT)["status"] == "applied"
    assert remote.applied == 1


def test_recall_prefix_briefing_and_retry_copies_are_invalidated_source_kept(setup):
    store, client, _remote = setup
    sid, tid = recorded(store, {"memories": [{"id": "mem-1", "text": "old fact"}]})
    copied = retry_copy(store, sid, tid)
    _, briefing = recorded(store, {"briefing": {"watch_flags": ["old fact"]}})
    _, ordinary = recorded(store)
    before = turn_context.replay(store, ordinary)
    messages = store.messages(sid)
    result = edits.apply(store, client, BODY)
    assert result["cachedPackagesCleared"] == 2
    for identity in (tid, copied, briefing):
        unavailable(store, identity)
    assert turn_context.replay(store, ordinary) == before
    assert store.messages(sid) == messages


def test_pending_hold_blocks_late_packages_but_allows_no_recall_conversation(setup):
    store, _client, _remote = setup
    unresolved(setup)
    sid = store.create_session()
    tid = store.start_turn(sid)
    memory_store.save_context(store, tid, "ok", {"memories": [{"text": "old"}]})
    assert memory_store.context(store, tid)["package"] is None
    with pytest.raises(context_controls.ContextError):
        turn_context.capture(
            store, tid, [], [Message("system", turn_context.RECALL_HEADER + "old")]
        )
    turn_context.capture(store, tid, [], [Message("system", "Normal no-memory instructions")])
    assert turn_context.load(store, tid)["prefix"][0]["content"] == "Normal no-memory instructions"


def test_correction_advances_ack_generation_and_fences_old_preparation(setup):
    store, client, _remote = setup
    with store._connect() as db:
        old_generation = memory_delivery.forget_generation(db)
    edits.apply(store, client, BODY)
    sid = store.create_session()
    tid = store.start_turn(sid)
    with pytest.raises(context_controls.ContextError):
        turn_context.capture(
            store,
            tid,
            [],
            [Message("system", memory_delivery.NOTE_PREFIX + "old acknowledgement")],
            acknowledgement_generation=old_generation,
        )


def test_preparing_turn_refuses_before_mutation(setup):
    store, client, remote = setup
    sid = store.create_session()
    with store._connect() as db:
        db.execute(
            "INSERT INTO turn_submissions(request_id,requested_session_id,state,history_seq,created_at,updated_at) VALUES('preparing_request_001',?,'preparing',0,0,0)",
            (sid,),
        )
    with pytest.raises(edits.EditError) as failure:
        edits.apply(store, client, BODY)
    assert failure.value.detail["code"] == "work_active"
    assert remote.applied == 0 and edits.pending(store)["pending"] is None
