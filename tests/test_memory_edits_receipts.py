"""Memory edits: receipts, bound destinations and identities, payload tampering and lost snapshots."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from waiting import WAIT, wait_event

from pi.memory import edits
from pi.memory import store as memory_store
from tests.memory_edits_helpers import (
    BODY,
    setup,  # noqa: F401 -- shared fixture
    unresolved,
)


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
        wait_event(release, "the test to release the held call")
        return snapshot

    client.memory = held
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(edits.apply, store, client, BODY)
        wait_event(entered, "the held call to start")
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
        assert future.result(WAIT)["status"] == "applied"
    with pytest.raises(edits.EditError) as stale:
        edits.apply(store, client, new)
    assert stale.value.detail["code"] == "revision_conflict" and remote.applied == 1
