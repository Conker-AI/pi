"""Owner memory edits that meet forgetting: holds, scrubbing, redaction and serialization with forget."""

import pytest

from pi import memory_edits as edits
from pi import memory_forget, memory_store
from tests.test_memory_edits import (
    BODY,
    setup,  # noqa: F401 - pytest fixture used by name
    unresolved,
)


def test_correction_and_forget_serialize_without_releasing_each_others_hold(setup):
    store, client, _remote = setup
    unresolved(setup)
    with pytest.raises(edits.EditError) as error:
        edits.apply(
            store,
            client,
            BODY.model_copy(update={"request_id": "another_request_0001", "expected_revision": 4}),
        )
    assert error.value.detail["code"] == "change_pending"
    with pytest.raises(memory_forget.ForgetError):
        memory_forget.forget(
            store,
            client,
            memory_forget.Forget(
                request_id="forget_request_0001", memory_id="mem-1", expected_revision=4
            ),
        )
    assert memory_store.recall_held(store) == 1


def source_memory(setup):
    from uuid import NAMESPACE_URL, uuid5

    store, _client, remote = setup
    sid = store.create_session()
    message = store.append_message(sid, "user", "Remember that this synthetic fact is private")
    receipt_id = str(uuid5(NAMESPACE_URL, "pi:companion:" + message["id"]))
    remote.memory_id = str(uuid5(NAMESPACE_URL, receipt_id + ":memory"))
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET destination_agent_id='companion',delivery_started=1 WHERE message_id=?",
            (message["id"],),
        )
    return sid, BODY.model_copy(update={"memory_id": remote.memory_id})


@pytest.mark.parametrize("legacy", [False, True])
def test_source_forget_scrubs_pending_text_and_late_receipt_cannot_restore_it(setup, legacy):
    from pi import forgetting

    store, client, remote = setup
    sid, body = source_memory(setup)
    remote.lose = True
    result = edits.apply(store, client, body)
    if legacy:
        with store._connect() as db:
            db.execute("UPDATE memory_outbox SET destination_agent_id=NULL")
    unrelated = store.create_session()
    store.append_message(unrelated, "user", "An unrelated message")
    with store._connect() as db:
        forgetting._redact(db, forgetting._plan(db, unrelated))
    assert edits.pending(store)["pending"] is not None
    with store._connect() as db:
        forgetting._redact(db, forgetting._plan(db, sid))
    resolved = edits.get(store, body.request_id)
    assert resolved["status"] == "rejected" and resolved["errorCode"] == "memory_forgotten"
    with store._connect() as db:
        assert db.execute("SELECT desired FROM memory_edit_operations").fetchone()[0] is None
    assert memory_store.recall_held(store)  # Source deletion still needs remote acknowledgement.
    before = len(remote.calls)
    assert edits.status(store, client, body.request_id) == resolved
    assert (
        edits.retry(
            store, client, body.request_id, edits.Retry(expected_payload_hash=result["payloadHash"])
        )
        == resolved
    )
    assert len(remote.calls) == before


def test_forget_between_snapshot_and_admission_cannot_restore_pending_text(setup):
    from pi import forgetting

    store, client, remote = setup
    sid, body = source_memory(setup)
    original = client.memory

    def interleave(identity):
        result = original(identity)
        with store._connect() as db:
            forgetting._redact(db, forgetting._plan(db, sid))
        return result

    client.memory = interleave
    with pytest.raises(edits.EditError) as failure:
        edits.apply(store, client, body)
    assert failure.value.detail["code"] == "not_found"
    assert edits.pending(store)["pending"] is None and remote.applied == 0


def test_late_apply_receipt_after_source_forget_keeps_terminal_redaction(setup):
    from pi import forgetting

    store, client, _remote = setup
    sid, body = source_memory(setup)
    original = client.apply

    def interleave(*args):
        result = original(*args)
        with store._connect() as db:
            forgetting._redact(db, forgetting._plan(db, sid))
        return result

    client.apply = interleave
    result = edits.apply(store, client, body)
    assert result["status"] == "rejected" and result["errorCode"] == "memory_forgotten"
    with store._connect() as db:
        assert db.execute("SELECT desired FROM memory_edit_operations").fetchone()[0] is None


def test_old_schema_source_forgetting_needs_no_new_runtime_start(setup):
    from pi import forgetting

    store, _client, _remote = setup
    sid = store.create_session()
    store.append_message(sid, "user", "A historical chat")
    with store._connect() as db:
        db.execute("DROP TABLE memory_edit_operations")
        result = forgetting._redact(db, forgetting._plan(db, sid))
    assert result
    assert store.get_session(sid)["status"] == "forgotten"


def test_new_edit_waits_for_existing_forget_hold(setup):
    store, client, remote = setup
    with store._connect() as db:
        db.execute(
            "INSERT INTO memory_forget_operations(request_id,memory_id,agent_id,destination,expected_revision,state) VALUES('forget_request_001','mem-2','companion',?,1,'pending')",
            (client.destination,),
        )
    with pytest.raises(edits.EditError) as failure:
        edits.apply(store, client, BODY)
    assert failure.value.detail["code"] == "change_pending"
    assert remote.applied == 0
