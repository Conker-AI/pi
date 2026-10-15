"""Synthetic correction, uncertainty and recall lifecycle; no provider/network work."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import httpx
import pytest
from pydantic import ValidationError

from pi import context_controls, memory_delivery, memory_store, turn_context
from pi import memory_edits as edits
from pi.memory_corrections import Client
from pi.providers import Message
from pi.store import Store

KEY = "synthetic-correction-capability-" + "k" * 32
BODY = edits.Apply(
    request_id="correction_request_0001",
    memory_id="mem-1",
    expected_revision=3,
    text="Owner-reviewed replacement 🐿",
)


class Remote:
    def __init__(self):
        self.memory_id = "mem-1"
        self.text, self.revision = "Original retained fact", 3
        self.receipts, self.calls, self.applied = {}, [], 0
        self.lose = self.before_dispatch = False
        self.reject = None
        self.malformed = False
        self.indexing = "indexed"

    def handle(self, request):
        assert request.headers["X-MemoryGate-Correction-Key"] == KEY
        assert request.headers["X-Agent-Id"] == "companion"
        assert "X-MemoryGate-Key" not in request.headers
        self.calls.append((request.method, request.url.path))
        identity = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and "/memories/" in request.url.path:
            if identity != self.memory_id:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": identity,
                    "agent_id": "companion",
                    "revision": self.revision,
                    "text": self.text,
                    "source_type": "inferred",
                    "confidence": "low",
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200 if identity in self.receipts else 404, json=self.receipts.get(identity, {})
            )
        if self.before_dispatch:
            raise httpx.ReadTimeout("PRIVATE_PROVIDER_ERROR")
        if self.reject:
            return httpx.Response(self.reject, json={"detail": "PRIVATE_PROVIDER_ERROR"})
        body = json.loads(request.content)
        assert set(body) == {"memory_id", "expected_revision", "text"}
        digest = edits.remote_digest("companion", edits.Apply(request_id=identity, **body))
        if identity in self.receipts:
            if self.receipts[identity]["payload_hash"] != digest:
                return httpx.Response(409)
            return httpx.Response(200, json=self.receipts[identity])
        if body["expected_revision"] != self.revision:
            return httpx.Response(409)
        self.text, self.revision = body["text"], self.revision + 1
        self.applied += 1
        self.receipts[identity] = {
            "request_id": identity,
            "memory_id": body["memory_id"],
            "agent_id": "companion",
            "previous_revision": self.revision - 1,
            "revision": self.revision,
            "status": "applied",
            "payload_hash": digest,
            "indexing": self.indexing,
        }
        if self.lose:
            raise httpx.ReadTimeout("PRIVATE_PROVIDER_ERROR")
        return httpx.Response(200, json={} if self.malformed else self.receipts[identity])


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


def unresolved(setup):
    store, client, remote = setup
    remote.lose = True
    result = edits.apply(store, client, BODY)
    assert result["status"] == "unknown"
    return result


def test_authoritative_snapshot_then_exact_apply_preserves_evidence_and_clears_payload(setup):
    store, client, remote = setup
    assert edits.preview(client, "mem-1") == {
        "schemaVersion": 1,
        "memoryId": "mem-1",
        "revision": 3,
        "text": "Original retained fact",
        "sourceType": "inferred",
        "confidence": "low",
    }
    result = edits.apply(store, client, BODY)
    assert result == {
        "schemaVersion": 1,
        "requestId": BODY.request_id,
        "memoryId": "mem-1",
        "previousRevision": 3,
        "payloadHash": edits._digest(BODY),
        "status": "applied",
        "revision": 4,
        "indexing": "indexed",
        "errorCode": None,
        "cachedPackagesCleared": 0,
        "sourceConversationKept": True,
    }
    assert edits.preview(client, "mem-1")["text"] == BODY.text
    assert edits.preview(client, "mem-1")["sourceType"] == "inferred"
    with store._connect() as db:
        assert db.execute("SELECT desired FROM memory_edit_operations").fetchone()[0] is None
    assert remote.applied == 1 and not memory_store.recall_held(store)
    assert edits.pending(store) == {"schemaVersion": 1, "pending": None}


def test_duplicate_submit_and_terminal_retry_never_repeat_write(setup):
    store, client, remote = setup
    result = edits.apply(store, client, BODY)
    count = len(remote.calls)
    assert edits.apply(store, client, BODY) == result
    assert (
        edits.retry(
            store, client, BODY.request_id, edits.Retry(expected_payload_hash=result["payloadHash"])
        )
        == result
    )
    assert edits.status(store, client, BODY.request_id) == result
    assert len(remote.calls) == count
    with pytest.raises(edits.EditError, match="already used"):
        edits.apply(store, client, BODY.model_copy(update={"text": "different"}))
    assert remote.applied == 1


def test_lost_reply_recovers_with_get_only_and_content_free_pending(setup):
    store, client, remote = setup
    result = unresolved(setup)
    assert memory_store.recall_held(store) == 1
    assert memory_store.pending_deletions(store) == 0  # Do not label correction as deletion.
    pending = edits.pending(store)
    assert pending["pending"] == {
        "requestId": BODY.request_id,
        "memoryId": "mem-1",
        "expectedRevision": 3,
        "payloadHash": result["payloadHash"],
        "status": "unknown",
    }
    assert BODY.text not in json.dumps(pending) and "PRIVATE" not in json.dumps(result)
    before = len(remote.calls)
    assert edits.apply(store, client, BODY)["status"] == "unknown"
    assert len(remote.calls) == before
    assert edits.status(store, client, BODY.request_id)["status"] == "applied"
    assert remote.calls[before:] == [("GET", "/runtime/corrections/" + BODY.request_id)]
    assert not memory_store.recall_held(store) and remote.applied == 1


def test_restart_missing_receipt_stays_unknown_until_explicit_exact_retry(setup):
    store, client, remote = setup
    remote.before_dispatch = True
    first = edits.apply(store, client, BODY)
    assert first["status"] == "unknown" and remote.applied == 0
    # Reopening/recovery cannot clear the hold or send a remote request.
    before = len(remote.calls)
    with closing(Store(store.path)) as reopened:
        edits.recover_interrupted(reopened)
        assert edits.pending(reopened)["pending"]["payloadHash"] == first["payloadHash"]
        assert len(remote.calls) == before
        assert edits.status(reopened, client, BODY.request_id)["status"] == "unknown"
        assert memory_store.recall_held(reopened)
        with pytest.raises(edits.EditError):
            edits.retry(
                reopened, client, BODY.request_id, edits.Retry(expected_payload_hash="0" * 64)
            )
        remote.before_dispatch = False
        assert (
            edits.retry(
                reopened,
                client,
                BODY.request_id,
                edits.Retry(expected_payload_hash=first["payloadHash"]),
            )["status"]
            == "applied"
        )
    assert remote.applied == 1 and remote.text == BODY.text


def test_crash_after_admission_remains_recoverable_without_automatic_dispatch(setup, monkeypatch):
    store, client, remote = setup

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(edits, "_dispatch", crash)
    with pytest.raises(RuntimeError):
        edits.apply(store, client, BODY)
    assert edits.pending(store)["pending"]["status"] == "applying"
    before = len(remote.calls)
    edits.recover_interrupted(store)
    assert edits.pending(store)["pending"]["status"] == "unknown"
    assert len(remote.calls) == before and memory_store.recall_held(store)


@pytest.mark.parametrize(
    "remote_status,code",
    [
        (400, "unavailable"),
        (401, "unavailable"),
        (403, "unavailable"),
        (404, "not_found"),
        (409, "revision_conflict"),
        (422, "unavailable"),
    ],
)
def test_fresh_definitive_refusal_releases_hold_and_text(setup, remote_status, code):
    store, client, remote = setup
    remote.reject = remote_status
    result = edits.apply(store, client, BODY)
    assert result["status"] == "rejected" and result["errorCode"] == code
    assert result["revision"] is None and not memory_store.recall_held(store)
    with store._connect() as db:
        assert db.execute("SELECT desired FROM memory_edit_operations").fetchone()[0] is None
    assert "PRIVATE" not in json.dumps(result)


def test_auth_refused_retry_does_not_resolve_previous_unknown(setup):
    store, client, remote = setup
    result = unresolved(setup)
    remote.reject = 401
    result = edits.retry(
        store, client, BODY.request_id, edits.Retry(expected_payload_hash=result["payloadHash"])
    )
    assert result["status"] == "unknown" and memory_store.recall_held(store)


@pytest.mark.parametrize("indexing", ["pending", "degraded", "indexed"])
def test_indexing_is_separate_from_authoritative_edit(setup, indexing):
    store, client, remote = setup
    remote.indexing = indexing
    result = edits.apply(store, client, BODY)
    assert result["status"] == "applied" and result["indexing"] == indexing
    assert not memory_store.recall_held(store)


def test_invalid_receipt_keeps_hold_and_can_reconcile(setup):
    store, client, remote = setup
    remote.malformed = True
    assert edits.apply(store, client, BODY)["status"] == "unknown"
    assert memory_store.recall_held(store)
    assert edits.status(store, client, BODY.request_id)["status"] == "applied"


@pytest.mark.parametrize(
    "field,value",
    [
        ("memory_id", "other"),
        ("previous_revision", True),
        ("revision", 7),
        ("agent_id", "other"),
        ("indexing", "unknown"),
    ],
)
def test_wrong_receipt_cannot_release_hold(setup, field, value):
    store, client, remote = setup
    unresolved(setup)
    remote.receipts[BODY.request_id][field] = value
    assert edits.status(store, client, BODY.request_id)["status"] == "unknown"
    assert memory_store.recall_held(store)


@pytest.mark.parametrize("changed", ["destination", "namespace"])
def test_bound_destination_namespace_checked_before_any_remote_read_or_write(setup, changed):
    store, client, remote = setup
    result = unresolved(setup)
    before = len(remote.calls)
    if changed == "destination":
        client.destination = "changed"
    else:
        client.agent_id = "other"
    for operation in (
        lambda: edits.status(store, client, BODY.request_id),
        lambda: edits.apply(store, client, BODY),
        lambda: edits.retry(
            store, client, BODY.request_id, edits.Retry(expected_payload_hash=result["payloadHash"])
        ),
    ):
        with pytest.raises(edits.EditError) as error:
            operation()
        assert error.value.detail["code"] == "request_conflict"
    assert len(remote.calls) == before


def test_foreign_memory_and_stale_revision_cannot_dispatch(setup):
    store, client, remote = setup
    for body, code in (
        (BODY.model_copy(update={"memory_id": "foreign"}), "not_found"),
        (BODY.model_copy(update={"expected_revision": 2}), "revision_conflict"),
    ):
        with pytest.raises(edits.EditError) as error:
            edits.apply(store, client, body)
        assert error.value.detail["code"] == code
    assert all(method == "GET" for method, _ in remote.calls)
    assert edits.pending(store)["pending"] is None


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
        assert release.wait(5)
        return original(*args)

    client.apply = blocked
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(edits.apply, store, client, BODY)
        assert entered.wait(5)
        try:
            assert memory_store.recall_held(store)
            with pytest.raises(edits.EditError) as error:
                edits.apply(store, client, BODY)
            assert error.value.detail["code"] == "in_progress"
        finally:
            release.set()
        assert future.result(5)["status"] == "applied"
    assert remote.applied == 1


def test_recall_prefix_briefing_and_retry_copies_are_invalidated_source_kept(setup):
    from test_memory_forget_recall import recorded, retry_copy, unavailable

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


@pytest.mark.parametrize(
    "update",
    [
        {"expected_revision": True},
        {"expected_revision": 2147483647},
        {"text": " "},
        {"text": "x\x00y"},
        {"text": "\ud800"},
        {"text": "x" * 16001},
        {"memory_id": "bad\n"},
        {"request_id": "request_identifier\n"},
        {"agent_id": "other"},
    ],
)
def test_strict_bounded_request(update):
    with pytest.raises(ValidationError):
        edits.Apply.model_validate({**BODY.model_dump(), **update})


def test_unicode_text_bound_is_codepoints_not_utf16_units(setup):
    store, client, remote = setup
    text = "🐿" * 16000
    assert edits.apply(store, client, BODY.model_copy(update={"text": text}))["status"] == "applied"
    assert remote.text == text


def test_explicit_retry_conflict_after_lost_dispatch_releases_hold(setup):
    store, client, remote = setup
    remote.before_dispatch = True
    value = edits.apply(store, client, BODY)
    remote.before_dispatch = False
    remote.revision += 1  # A different owner operation edited the record meanwhile.
    result = edits.retry(
        store, client, BODY.request_id, edits.Retry(expected_payload_hash=value["payloadHash"])
    )
    assert result["status"] == "rejected" and result["errorCode"] == "revision_conflict"
    assert remote.applied == 0 and not memory_store.recall_held(store)
    with store._connect() as db:
        assert db.execute("SELECT desired FROM memory_edit_operations").fetchone()[0] is None


@pytest.mark.parametrize("later", ["edit", "forget"])
def test_prior_matching_commit_still_replays_after_later_changes(setup, later):
    store, client, remote = setup
    value = unresolved(setup)
    if later == "edit":
        remote.revision += 1
        remote.text = "Another later edit"
    else:
        remote.memory_id = "deleted"
    result = edits.retry(
        store, client, BODY.request_id, edits.Retry(expected_payload_hash=value["payloadHash"])
    )
    assert result["status"] == "applied" and result["revision"] == 4
    assert remote.applied == 1 and not memory_store.recall_held(store)


def test_same_identity_target_and_revisions_different_text_cannot_false_acknowledge(setup):
    store, client, remote = setup
    remote.before_dispatch = True
    value = edits.apply(store, client, BODY)
    remote.before_dispatch = False
    different = BODY.model_copy(update={"text": "Another writer's text"})
    client.apply(
        different.request_id, different.memory_id, different.expected_revision, different.text
    )
    assert edits.status(store, client, BODY.request_id)["status"] == "unknown"
    assert memory_store.recall_held(store)
    result = edits.retry(
        store, client, BODY.request_id, edits.Retry(expected_payload_hash=value["payloadHash"])
    )
    assert result["status"] == "rejected" and result["errorCode"] == "revision_conflict"
    assert remote.text == different.text and remote.applied == 1


def test_receipt_without_payload_proof_keeps_unknown_on_put_and_get(setup):
    store, client, remote = setup
    original = client.apply

    def old_service(*args):
        receipt = original(*args)
        receipt.pop("payload_hash")
        return receipt

    client.apply = old_service
    assert edits.apply(store, client, BODY)["status"] == "unknown"
    remote.receipts[BODY.request_id].pop("payload_hash")
    assert edits.status(store, client, BODY.request_id)["status"] == "unknown"
    assert memory_store.recall_held(store)


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


def test_saved_payload_tampering_cannot_change_exact_retry(setup):
    store, client, remote = setup
    result = unresolved(setup)
    with store._connect() as db:
        db.execute("UPDATE memory_edit_operations SET desired='Unapproved text'")
    before = len(remote.calls)
    with pytest.raises(edits.EditError):
        edits.retry(
            store, client, BODY.request_id, edits.Retry(expected_payload_hash=result["payloadHash"])
        )
    assert len(remote.calls) == before


def test_delayed_original_snapshot_cannot_overlap_new_approval_after_status404(setup):
    store, client, remote = setup
    entered, release = Event(), Event()
    original = client.memory

    def held(identity):
        snapshot = original(identity)
        entered.set()
        assert release.wait(5)
        return snapshot

    client.memory = held
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(edits.apply, store, client, BODY)
        assert entered.wait(5)
        try:
            with pytest.raises(edits.EditError) as unknown:
                edits.status(store, client, BODY.request_id)
            assert unknown.value.status == 404
            new = BODY.model_copy(update={"request_id": "new_approval_request_001"})
            with pytest.raises(edits.EditError) as pending:
                edits.apply(store, client, new)
            assert pending.value.detail["code"] == "in_progress"
        finally:
            release.set()
        assert future.result(5)["status"] == "applied"
    with pytest.raises(edits.EditError) as stale:
        edits.apply(store, client, new)
    assert stale.value.detail["code"] == "revision_conflict" and remote.applied == 1
