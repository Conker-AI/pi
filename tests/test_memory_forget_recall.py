"""Forget fences derived recall; exact retries cannot revive it. No external I/O."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_memory_forget import BODY, client, forgotten

from pi.actions import identities as actions
from pi.api import browser_contract
from pi.context import controls as context_controls
from pi.conversation import drafts, forgetting, submissions
from pi.conversation.store import Store
from pi.memory import forget as memory_forget
from pi.memory import store as memory_store
from pi.memory.service import Memory
from pi.models.providers import Message
from pi.models.routing import Router
from pi.turns import context as turn_context
from pi.turns.loop import Loop

SECRET = "SYNTHETIC_RECALLED_FACT"


def test_forget_revision_matches_memorygate_bounds_before_storage():
    assert (
        memory_forget.Forget(
            **{**BODY.model_dump(), "expected_revision": 2147483646}
        ).expected_revision
        == 2147483646
    )
    for revision in (0, 2147483647, 2**63):
        with pytest.raises(ValidationError):
            memory_forget.Forget(**{**BODY.model_dump(), "expected_revision": revision})


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def recorded(store, package=None, sid=None, final=None):
    sid = sid or store.create_session()
    tid = store.start_turn(sid)
    store.append_message(sid, "user", "An ordinary request", turn_id=tid, purpose="input")
    if package is not None:
        memory_store.save_context(store, tid, "ok", package)
    Loop(store, Router())._history(sid, turn_id=tid)
    if final is None:
        store.finish_turn(tid, "complete")
    else:
        store.complete_turn(tid, final)
    return sid, tid


def recalled():
    return {"memories": [{"id": BODY.memory_id, "text": SECRET}], "retrieval": {}}


def retry_copy(store, sid, tid):
    copied = store.start_turn(sid)
    with store._connect() as db:
        db.execute(
            "INSERT INTO turn_context_inputs SELECT ?,source_ids,prefix,reply_to,captured_at "
            "FROM turn_context_inputs WHERE turn_id=?",
            (copied, tid),
        )
    store.finish_turn(copied, "complete")
    return copied


def unavailable(store, tid):
    with pytest.raises(context_controls.ContextError) as failure:
        turn_context.replay(store, tid)
    assert failure.value.detail["code"] == "context_unavailable"


def test_record_briefing_and_retry_copies_are_invalidated_but_no_recall_snapshot_survives(store):
    sid, tid = recorded(store, recalled())
    copied = retry_copy(store, sid, tid)
    _, briefing = recorded(
        store, {"memories": [], "briefing": {"watch_flags": [SECRET]}, "retrieval": {}}
    )
    _, independent = recorded(store)
    before = turn_context.replay(store, independent)
    receipt = memory_forget.forget(store, client(forgotten), BODY)
    assert receipt["cachedPackagesCleared"] == 2
    for identity in (tid, copied, briefing):
        unavailable(store, identity)
    assert turn_context.replay(store, independent) == before
    assert memory_store.context(store, briefing)["package"] is None
    with store._connect() as db:
        assert (
            db.execute("SELECT 1 FROM memory_contexts WHERE turn_id=?", (copied,)).fetchone()
            is None
        )
        assert not any(
            SECRET in (row[0] or "") for row in db.execute("SELECT prefix FROM turn_context_inputs")
        )
        # The temporary invalidation exception restores completed-context immutability.
        with pytest.raises(Exception, match="immutable"):
            db.execute("UPDATE turn_context_inputs SET prefix='[]' WHERE turn_id=?", (independent,))


def test_source_messages_draft_summary_and_action_records_are_retained(store):
    sid, tid = recorded(store, recalled())
    store.append_message(sid, "user", SECRET)
    store.append_message(sid, "assistant", "Conversation answer: " + SECRET)
    store.append_message(sid, "tool", {"result": SECRET})
    draft = drafts.save(store, sid, drafts.Save(expected_revision=0, text=SECRET))
    with store._connect() as db:
        db.execute("UPDATE sessions SET summary=? WHERE id=?", (SECRET, sid))
        db.execute(
            "INSERT INTO tool_actions VALUES('act_source',?,'tool',?,NULL,'completed',0)",
            (tid, json.dumps({"owner_selected": SECRET})),
        )
    messages, action = store.messages(sid), actions.latest(store, tid)
    memory_forget.forget(store, client(forgotten), BODY)
    assert store.messages(sid) == messages
    assert actions.latest(store, tid) == action
    assert drafts.load(store, sid) == draft
    assert store.get_session(sid)["summary"] == SECRET


@pytest.mark.parametrize("preparing", [False, True])
def test_active_work_is_rejected_before_remote_mutation_or_cache_invalidation(store, preparing):
    _, cached = recorded(store, recalled())
    sid = store.create_session()
    if preparing:
        submissions.reserve(store, "preparing_request_0001", sid, "New request", {})
    else:
        store.start_turn(sid)
    calls = []
    with pytest.raises(memory_forget.ForgetError) as failure:
        memory_forget.forget(store, client(lambda request: calls.append(request)), BODY)
    assert failure.value.status == 409 and failure.value.detail["code"] == "work_active"
    assert calls == []
    assert memory_store.context(store, cached)["package"] == recalled()
    assert memory_forget.pending(store) == {"schemaVersion": 1, "pending": None}


def test_remote_hold_allows_new_chat_without_recall_and_blocks_late_snapshot_capture(store):
    _, cached = recorded(store, recalled())

    class NoRead:
        def retrieve(self, *args, **kwargs):
            pytest.fail("Recall must be paused during a pending forget")

    def during_forget(request):
        assert memory_store.pending_deletions(store) == 1
        unavailable(store, cached)
        sid = store.create_session()
        tid = store.start_turn(sid)
        store.append_message(sid, "user", "Still talk with me", turn_id=tid, purpose="input")
        Memory(store, NoRead()).prepare(tid, "Current request")
        messages = Loop(store, Router())._history(sid, turn_id=tid)
        assert all(SECRET not in message.content for message in messages)
        assert memory_store.context(store, tid)["status"] == "unavailable"
        with pytest.raises(context_controls.ContextError) as failure:
            turn_context.capture(
                store, tid, [], [Message("system", turn_context.RECALL_HEADER + SECRET)]
            )
        assert failure.value.detail["code"] == "memory_forgetting"
        store.finish_turn(tid, "complete")
        # An old retrieval response cannot be installed while the durable hold is active.
        late = store.start_turn(store.create_session())
        memory_store.save_context(store, late, "ok", recalled())
        assert memory_store.context(store, late)["package"] is None
        assert memory_store.context(store, late)["status"] == "unavailable"
        store.finish_turn(late, "complete")
        return forgotten(request)

    memory_forget.forget(store, client(during_forget), BODY)
    assert memory_store.pending_deletions(store) == 0


def test_unknown_outcome_survives_restart_and_only_exact_recovery_releases_hold(tmp_path):
    path = tmp_path / "restart.db"

    def timeout(request):
        raise httpx.ReadTimeout("Never expose transport details")

    with closing(Store(path)) as store:
        _, tid = recorded(store, recalled())
        with pytest.raises(memory_forget.ForgetError) as failure:
            memory_forget.forget(store, client(timeout), BODY)
        assert failure.value.detail["code"] == "outcome_unknown"
        assert "Never expose" not in str(failure.value.detail)
    with closing(Store(path)) as store:
        assert memory_forget.pending(store) == {
            "schemaVersion": 1,
            "pending": {"requestId": BODY.request_id, "memoryId": BODY.memory_id, "revision": 2},
        }
        unavailable(store, tid)
        with pytest.raises(memory_forget.ForgetError) as refusal:
            memory_forget.forget(store, client(lambda request: httpx.Response(401)), BODY)
        assert refusal.value.detail["code"] == "outcome_unknown"
        assert memory_store.pending_deletions(store) == 1
        changed = BODY.model_copy(update={"memory_id": "other"})
        with pytest.raises(memory_forget.ForgetError) as rebound:
            memory_forget.forget(store, client(forgotten), changed)
        assert rebound.value.detail["code"] == "request_conflict"
        different = BODY.model_copy(update={"request_id": "different_request_0001"})
        with pytest.raises(memory_forget.ForgetError) as conflict:
            memory_forget.forget(store, client(forgotten), different)
        assert conflict.value.detail["code"] == "forget_pending"
        receipt = memory_forget.forget(store, client(forgotten), BODY)
        assert memory_store.pending_deletions(store) == 0
        assert memory_forget.pending(store)["pending"] is None
        assert (
            memory_forget.forget(
                store, client(lambda request: pytest.fail("No replay needed")), BODY
            )
            == receipt
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "different_request_0001"),
        ("memory_id", "other"),
        ("agent_id", "other"),
        ("revision", 3),
        ("revision", True),
        ("status", "available"),
        ("index_removal", "unexpected"),
    ],
)
def test_wrong_receipt_never_releases_hold(store, field, value):
    def wrong(request):
        result = forgotten(request).json()
        result[field] = value
        return httpx.Response(200, json=result)

    with pytest.raises(memory_forget.ForgetError) as failure:
        memory_forget.forget(store, client(wrong), BODY)
    assert failure.value.detail["code"] == (
        "outcome_unknown" if field == "agent_id" else "invalid_receipt"
    )
    assert memory_store.pending_deletions(store) == 1


def test_concurrent_duplicate_never_overlaps_remote_dispatch(store):
    entered, release = Event(), Event()
    calls = []

    def wait(request):
        calls.append(request)
        entered.set()
        assert release.wait(5)
        return forgotten(request)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(memory_forget.forget, store, client(wait), BODY)
        assert entered.wait(5)
        try:
            with pytest.raises(memory_forget.ForgetError) as failure:
                memory_forget.forget(store, client(wait), BODY)
            assert failure.value.detail["code"] == "forget_in_progress"
        finally:
            release.set()
        assert future.result()["status"] == "forgotten"
    assert len(calls) == 1


def test_source_chat_forget_invalidates_recall_in_other_chat_without_forgetting_that_chat(store):
    source = store.create_session()
    message = store.append_message(source, "user", SECRET)
    package = recalled()
    package["memories"][0]["citations"] = [{"session_id": source, "message_id": message["id"]}]
    other, tid = recorded(store, package)
    copied = retry_copy(store, other, tid)
    before = store.messages(other)
    with store._connect() as db:
        plan = forgetting._plan(db, source)
        forgetting._redact(db, plan)
    assert store.get_message(message["id"])["content_status"] == "forgotten"
    assert store.messages(other) == before and store.get_session(other)["status"] == "open"
    unavailable(store, tid)
    unavailable(store, copied)


def test_recovery_route_is_owner_only():
    import re

    path = "/memory/forget-pending"
    assert any(re.fullmatch(pattern, path) for pattern in browser_contract.OWNER_ROUTES["GET"])
    assert not any(
        re.fullmatch(pattern, path) for pattern in browser_contract.RUNTIME_ROUTES["GET"]
    )


def test_pending_http_read_is_content_free_owner_control_and_needs_no_remote_record(
    store, monkeypatch
):
    from pi.api import server as api

    owner = "synthetic_owner_control_key"
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic_admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(owner.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(api.app.state, "memory_corrections", None, raising=False)
    http = TestClient(api.app)
    assert http.get("/memory/forget-pending").status_code == 401
    assert http.get("/memory/forget-pending", headers={"X-Pi-Key": "runtime"}).status_code == 401
    headers = {"X-Pi-Owner-Key": owner}
    assert http.get("/memory/forget-pending", headers=headers).json() == {
        "schemaVersion": 1,
        "pending": None,
    }
    with pytest.raises(memory_forget.ForgetError):
        memory_forget.forget(store, client(lambda request: httpx.Response(500)), BODY)
    response = http.get("/memory/forget-pending", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "schemaVersion": 1,
        "pending": {"requestId": BODY.request_id, "memoryId": BODY.memory_id, "revision": 2},
    }


def test_pending_request_cannot_be_rebound_after_namespace_configuration_changes(store):
    with pytest.raises(memory_forget.ForgetError):
        memory_forget.forget(store, client(lambda request: httpx.Response(500)), BODY)
    changed = client(lambda request: pytest.fail("Never target another namespace"))
    changed.agent_id = "other_namespace"
    with pytest.raises(memory_forget.ForgetError) as failure:
        memory_forget.forget(store, changed, BODY)
    assert failure.value.detail["code"] == "request_conflict"
    assert memory_store.pending_deletions(store) == 1


def test_empty_recall_notice_has_no_forgotten_evidence_and_remains_exactly_replayable(store):
    _, tid = recorded(store, {"memories": [], "briefing": {}, "retrieval": {}})
    before = turn_context.replay(store, tid)
    memory_forget.forget(store, client(forgotten), BODY)
    assert memory_store.context(store, tid)["package"] is None
    assert turn_context.replay(store, tid) == before


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("url", ["https://another-memorygate", "http://memorygate/other-base"])
def test_request_pins_destination_for_pending_and_completed_identity(store, complete, url):
    from pi.memory.corrections import Client

    if complete:
        memory_forget.forget(store, client(forgotten), BODY)
    else:
        with pytest.raises(memory_forget.ForgetError):
            memory_forget.forget(store, client(lambda request: httpx.Response(500)), BODY)
    changed = Client(
        url,
        "synthetic-correction-key-for-tests-12345",
        "owner",
        transport=httpx.MockTransport(lambda request: pytest.fail("No other destination")),
    )
    with pytest.raises(memory_forget.ForgetError) as failure:
        memory_forget.forget(store, changed, BODY)
    assert failure.value.detail["code"] == "request_conflict"
    assert bool(memory_store.pending_deletions(store)) == (not complete)


def test_key_rotation_at_same_destination_can_reconcile(store):
    from pi.memory.corrections import Client

    with pytest.raises(memory_forget.ForgetError):
        memory_forget.forget(store, client(lambda request: httpx.Response(500)), BODY)
    rotated = Client(
        "http://memorygate/",
        "rotated-synthetic-correction-key-12345",
        "owner",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "request_id": BODY.request_id,
                    "agent_id": "owner",
                    "memory_id": BODY.memory_id,
                    "revision": BODY.expected_revision,
                    "status": "forgotten",
                    "index_removal": "removed",
                },
            )
        ),
    )
    assert memory_forget.forget(store, rotated, BODY)["status"] == "forgotten"
    assert not memory_store.pending_deletions(store)


def test_forget_between_retry_read_and_admission_cannot_recreate_saved_prefix(store, monkeypatch):
    from test_response_retries import body, setup

    from pi.conversation import retries as response_retries

    loop, first, second = setup(store)
    sid, tid = recorded(store, recalled(), final="Original answer")
    final = store.messages(sid)[-1]
    original_replay = turn_context.replay
    intervened = []

    def intervene(*args):
        prepared = original_replay(*args)
        if not intervened:
            intervened.append(memory_forget.forget(store, client(forgotten), BODY))
        return prepared

    monkeypatch.setattr(turn_context, "replay", intervene)
    with pytest.raises(context_controls.ContextError) as failure:
        response_retries.run(loop, sid, final["id"], body())
    assert failure.value.detail["code"] == "context_unavailable"
    assert not first.calls and not second.calls
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM response_retries").fetchone()[0] == 0
        assert (
            db.execute("SELECT prefix FROM turn_context_inputs WHERE turn_id=?", (tid,)).fetchone()[
                0
            ]
            is None
        )
