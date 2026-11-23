"""Synthetic correction, uncertainty and recall lifecycle; no provider/network work."""

import json
from contextlib import closing

import pytest
from pydantic import ValidationError

from pi.conversation.store import Store
from pi.memory import edits
from pi.memory import store as memory_store
from tests.memory_edits_helpers import (
    BODY,
    setup,  # noqa: F401 -- shared fixture
    unresolved,
)


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
