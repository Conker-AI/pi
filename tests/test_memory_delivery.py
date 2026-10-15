"""Saved conversation is separate from source-bound, revocable memory acknowledgements."""

import hashlib
import json
from contextlib import closing
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient

from pi import (
    agents,
    api,
    browser_contract,
    context_controls,
    forgetting,
    memory_delivery,
    memory_forget,
    memory_store,
    session_settings,
    turn_context,
)
from pi.loop import Loop
from pi.memory import Memory, MemoryClient
from pi.memory_corrections import Client as CorrectionClient
from pi.providers import Message
from pi.routing import Router
from pi.store import Store

DESTINATION = memory_delivery.destination_digest(httpx.URL("http://memory.test"))


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def message(store, text="Remember that my name is Mira", session=None, turn=None):
    return store.append_message(
        session or store.create_session(),
        "user",
        text,
        turn_id=turn,
        **({"purpose": "input"} if turn else {}),
    )


def receipt(item, *, state="admitted", method="owner_requested", agent="default", stamp=True):
    rid = str(uuid5(NAMESPACE_URL, f"pi:{agent}:{item['id']}"))
    result = {
        "id": rid,
        "message_id": item["id"],
        "state": state,
        "memory_id": str(uuid5(NAMESPACE_URL, rid + ":memory")) if state == "admitted" else None,
        "value_score": 0.0,
        "admission_method": method,
    }
    if stamp:
        result.update(receipt_version=1, destination=DESTINATION)
    return result


def save_ack(store, item, value=None, **kwargs):
    value = receipt(item, **kwargs) if value is None else value
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET state='sent',receipt=?,destination_agent_id='default',"
            "delivery_started=1 WHERE message_id=?",
            (json.dumps(value), item["id"]),
        )
    return value


def projected(store, item):
    return store.get_message(item["id"])["memory_delivery"]


def forget_record(store, item, *, state="pending", agent="default", destination=DESTINATION):
    with store._connect() as db:
        db.execute(
            "INSERT INTO memory_forget_operations(request_id,memory_id,agent_id,destination,"
            "expected_revision,state) VALUES('forget_delivery_0001',?,?,?,?,?)",
            (receipt(item)["memory_id"], agent, destination, 1, state),
        )


def test_durable_ack_only_after_validated_delivery_and_local_destination_stamp(store):
    item = message(store)
    assert projected(store, item) == {
        "state": "pending",
        "reason_code": "queued",
        "admission_method": None,
        "memory_id": None,
    }
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["X-Agent-Id"] == "default"
        assert json.loads(request.content)["content"] == item["content"]
        # The remote endpoint cannot dictate its local destination identity.
        return httpx.Response(200, json={**receipt(item), "destination": "remote-untrusted"})

    client = MemoryClient(
        "http://memory.test", "ingest-test", "read-test", transport=httpx.MockTransport(handler)
    )
    with closing(Memory(store, client)) as memory:
        assert memory.drain_once() == 1
    assert len(calls) == 1
    assert projected(store, item) == {
        "state": "saved",
        "reason_code": None,
        "admission_method": "owner_requested",
        "memory_id": receipt(item)["memory_id"],
    }
    with store._connect() as db:
        saved = json.loads(db.execute("SELECT receipt FROM memory_outbox").fetchone()[0])
        assert saved["destination"] == DESTINATION and saved["receipt_version"] == 1
    assert store.get_message(item["id"])["content"] == item["content"]
    other = store.append_message(item["session_id"], "assistant", "I remember")
    assert "memory_delivery" not in store.get_message(other["id"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "wrong"),
        ("message_id", "foreign"),
        ("memory_id", "plausible-id"),
        ("memory_id", None),
        ("state", "queued"),
        ("state", []),
        ("admission_method", "filtered"),
        ("admission_method", []),
        ("value_score", True),
        ("value_score", float("nan")),
        ("value_score", 2),
    ],
)
def test_source_bound_receipts_refuse_malformed_acknowledgements(store, field, value):
    item = message(store)
    body = {**receipt(item), field: value}
    with pytest.raises(ValueError):
        memory_delivery.validate_receipt(body, item["id"], "default")
    save_ack(store, item, body)
    assert projected(store, item)["reason_code"] == "invalid_receipt"


@pytest.mark.parametrize("body", [None, [], {"state": "admitted"}, {"padding": "x" * 17000}])
def test_bad_transport_ack_keeps_delivery_pending_without_saved_claim(store, body):
    item = message(store)
    client = MemoryClient(
        "http://memory.test",
        "ingest",
        "read",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body),
        ),
    )
    with closing(Memory(store, client)) as memory:
        assert memory.drain_once() == 0
    assert projected(store, item)["state"] in {"pending", "unavailable"}
    assert projected(store, item)["memory_id"] is None


@pytest.mark.parametrize(
    "state,method,expected",
    [
        ("admitted", None, "saved"),
        ("filtered", None, "filtered"),
        ("filtered", "filtered", "filtered"),
        ("deleted", "owner_requested", "removed"),
    ],
)
def test_legacy_method_is_unknown_and_terminal_states_remain_honest(store, state, method, expected):
    item = message(store)
    save_ack(store, item, state=state, method=method, stamp=False)
    view = projected(store, item)
    assert view["state"] == expected
    assert view["admission_method"] == (method if expected != "removed" else None)
    if expected != "saved":
        assert view["memory_id"] is None


@pytest.mark.parametrize("state,reason", [("pending", "retry_scheduled"), ("blocked", "blocked")])
def test_pending_and_blocked_are_durable_without_exposing_raw_error(store, state, reason):
    item = message(store)
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET state=?,attempts=2,error='PRIVATE ERROR'", (state,))
    assert projected(store, item)["reason_code"] == reason
    assert "PRIVATE" not in json.dumps(memory_delivery.read(store, item["session_id"]))


@pytest.mark.parametrize(
    "state,expected,reason",
    [
        ("pending", "pending", "forget_pending"),
        ("forgotten", "removed", "forgotten"),
        ("rejected", "saved", None),
    ],
)
def test_individual_forget_masks_historical_admission_without_erasing_chat(
    store, state, expected, reason
):
    item = message(store)
    save_ack(store, item)
    forget_record(store, item, state=state)
    view = projected(store, item)
    assert view["state"] == expected and view["reason_code"] == reason
    assert store.get_message(item["id"])["content"] == item["content"]
    if expected != "saved":
        assert view["memory_id"] is None and view["admission_method"] is None


@pytest.mark.parametrize("agent,destination", [("other", DESTINATION), ("default", "0" * 64)])
def test_forget_identity_and_destination_do_not_mask_an_unrelated_memory(store, agent, destination):
    item = message(store)
    save_ack(store, item)
    forget_record(store, item, state="forgotten", agent=agent, destination=destination)
    assert projected(store, item)["state"] == "saved"


def test_legacy_unknown_destination_never_claims_saved_after_possible_forget(store):
    item = message(store)
    save_ack(store, item, stamp=False, method=None)
    forget_record(store, item, state="forgotten")
    assert projected(store, item) == {
        "state": "unavailable",
        "reason_code": "destination_unknown",
        "admission_method": None,
        "memory_id": None,
    }


def test_original_message_privacy_survives_future_setting_changes(store):
    sid = store.create_session()

    def privacy(disabled, revision):
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=revision,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(
                        memoryDisabled=disabled, harnessDisabled=False
                    ),
                ),
            ),
        )

    privacy(True, 0)
    excluded = message(store, session=sid)
    privacy(False, 1)
    eligible = message(store, session=sid)
    assert projected(store, excluded)["reason_code"] == "privacy_disabled"
    assert projected(store, eligible)["state"] == "pending"


def test_specialist_exclusion_is_not_reclassified_after_switch_to_companion(store):
    from test_session_settings import change, config

    specialist = agents.create(store, config())
    sid = store.create_session(agent_id=specialist["id"])
    excluded = message(store, session=sid)
    change(store, sid, agent="companion")
    assert projected(store, excluded) == {
        "state": "excluded",
        "reason_code": "agent_excluded",
        "admission_method": None,
        "memory_id": None,
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"state":',
        "[]",
        '"unknown"',
        '{"padding":"' + "x" * 17000 + '"}',
        "[" * 1100 + "0" + "]" * 1100,
    ],
)
def test_malformed_persisted_receipt_cannot_break_conversation_reads(store, raw, monkeypatch):
    item = message(store)
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET state='sent',destination_agent_id='default',receipt=?", (raw,)
        )
    rows = store.messages(item["session_id"])
    assert rows[0]["content"] == item["content"]
    assert rows[0]["memory_delivery"]["reason_code"] == "invalid_receipt"
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "loop", Loop(store, Router()), raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
    response = TestClient(api.app).get(
        f"/sessions/{item['session_id']}", headers={"X-Pi-Key": "recovery-key"}
    )
    assert response.status_code == 200
    assert response.json()["messages"][0]["content"] == item["content"]
    assert response.json()["memory"]["admission"]["admitted"] == 0


def test_real_individual_forget_revokes_note_and_badge_but_keeps_source_message(store):
    sid, tid, item = prepared(store, acknowledged=True)
    Loop(store, Router())._history(sid, turn_id=tid)
    store.complete_turn(tid, "Original answer")
    mid = receipt(item)["memory_id"]
    request_id = "forget_delivery_0001"

    def remote(request):
        assert json.loads(request.content)["memory_id"] == mid
        return httpx.Response(
            200,
            json={
                "request_id": request_id,
                "memory_id": mid,
                "agent_id": "default",
                "revision": 1,
                "status": "forgotten",
                "index_removal": "removed",
            },
        )

    with closing(
        CorrectionClient(
            "http://memory.test",
            "synthetic-key-for-tests-1234567890",
            "default",
            transport=httpx.MockTransport(remote),
        )
    ) as correction:
        memory_forget.forget(
            store,
            correction,
            memory_forget.Forget(request_id=request_id, memory_id=mid, expected_revision=1),
        )
    assert projected(store, item)["state"] == "removed"
    assert store.get_message(item["id"])["content"] == item["content"]
    with pytest.raises(context_controls.ContextError):
        turn_context.replay(store, tid)


def test_metadata_read_latest100_is_stable_bounded_and_specific_old_read_is_available(store):
    sid = store.create_session()
    items = [message(store, f"PRIVATE content {index}", sid) for index in range(102)]
    assistant = store.append_message(sid, "assistant", "PRIVATE reply")
    result = memory_delivery.read(store, sid)
    assert result["truncated"] is True
    assert [row["message_id"] for row in result["results"]] == [item["id"] for item in items[2:]]
    assert "PRIVATE" not in json.dumps(result) and "value_score" not in json.dumps(result)
    assert (
        memory_delivery.read(store, sid, items[0]["id"])["results"][0]["message_id"]
        == items[0]["id"]
    )
    foreign = message(store)
    for identity in (assistant["id"], foreign["id"], "missing"):
        with pytest.raises(ValueError):
            memory_delivery.read(store, sid, identity)


def test_api_runtime_auth_query_rejection_and_forgotten_session_masking(store, monkeypatch):
    item = message(store)
    save_ack(store, item)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime-key").hexdigest(), raising=False
    )
    headers = {"X-Pi-Gateway-Key": "runtime-key"}
    paths = [
        f"/sessions/{item['session_id']}/memory-delivery",
        f"/sessions/{item['session_id']}/messages/{item['id']}/memory-delivery",
    ]
    http = TestClient(api.app)
    for path in paths:
        assert browser_contract.runtime_allowed("GET", path)
        assert not browser_contract.runtime_allowed("POST", path)
        assert http.get(path).status_code == 401
        assert http.get(path, headers=headers).json()["results"][0]["state"] == "saved"
        assert http.get(path + "?query=private", headers=headers).status_code == 422
    assert http.get("/sessions/missing/memory-delivery", headers=headers).status_code == 404
    assert (
        http.get(
            paths[0].replace(item["session_id"], ".bad"), headers={"X-Pi-Key": "recovery-key"}
        ).status_code
        == 422
    )
    plan = forgetting.preview(store.path, item["session_id"])
    store.close()
    forgetting.forget(store.path, item["session_id"], plan["confirmation"])
    with closing(Store(store.path)) as reopened:
        monkeypatch.setattr(api.app.state, "store", reopened)
        for path in paths:
            result = http.get(path, headers=headers).json()
            assert (
                result["content_status"] == "forgotten"
                and result["results"] == []
                and result["truncated"] is False
            )
        assert projected(reopened, item)["state"] == "removed"


def test_destination_stamp_matches_correction_client_including_base_path():
    with closing(
        CorrectionClient(
            "http://memory.test:8080/base", "synthetic-key-for-tests-1234567890", "default"
        )
    ) as correction:
        assert correction.destination == memory_delivery.destination_digest(
            httpx.URL("http://memory.test:8080/base/")
        )


def prepared(store, *, text="Remember that my name is Mira", acknowledged=False):
    sid = store.create_session()
    tid = store.start_turn(sid)
    item = message(store, text, sid, tid)
    if acknowledged:
        save_ack(store, item)
    return sid, tid, item


@pytest.mark.parametrize("acknowledged", [False, True])
def test_remember_note_uses_only_existing_ack_and_is_invalidated_with_exact_retry_snapshot(
    store, acknowledged
):
    sid, tid, item = prepared(store, acknowledged=acknowledged)
    messages = Loop(store, Router())._history(sid, turn_id=tid)
    notes = [m.content for m in messages if m.content.startswith(memory_delivery.NOTE_PREFIX)]
    assert len(notes) == 1
    assert ("past save" if acknowledged else "not been acknowledged") in notes[0]
    store.complete_turn(tid, "Answer")
    saved = turn_context.load(store, tid)
    assert turn_context.replay(store, tid)["source"] == saved
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        memory_store.invalidate_recall(db)
        db.commit()
    with pytest.raises(context_controls.ContextError):
        turn_context.replay(store, tid)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(context_controls.ContextError):
            turn_context.require_snapshot(db, saved)
    assert store.get_message(item["id"])["content"] == item["content"]


def test_pending_forget_blocks_dynamic_note_capture_but_safe_current_chat_can_continue(store):
    sid, tid, item = prepared(store, acknowledged=True)
    note = memory_delivery.prompt_note(store, tid)
    forget_record(store, item)
    with pytest.raises(context_controls.ContextError) as failure:
        turn_context.capture(store, tid, [item], [Message("system", note)])
    assert failure.value.detail["code"] == "memory_forgetting"
    messages = Loop(store, Router())._history(sid, turn_id=tid)
    assert not any(m.content.startswith(memory_delivery.NOTE_PREFIX) for m in messages)
    assert any("No memory save acknowledgement is supplied" in m.content for m in messages)


def test_completed_forget_between_note_read_and_capture_refuses_stale_save_ack(store):
    _, tid, item = prepared(store, acknowledged=True)
    note, generation = memory_delivery.prepare_note(store, tid)
    forget_record(store, item, state="forgotten")
    with pytest.raises(context_controls.ContextError) as failure:
        turn_context.capture(
            store, tid, [item], [Message("system", note)], acknowledgement_generation=generation
        )
    assert failure.value.detail["code"] == "context_unavailable"


def test_normal_outbox_progress_does_not_change_model_or_snapshot_note(store, monkeypatch):
    sid, tid, item = prepared(store)
    original = memory_delivery.prepare_note

    def delivered_after_prepare(*args):
        note = original(*args)
        save_ack(store, item)
        return note

    monkeypatch.setattr(memory_delivery, "prepare_note", delivered_after_prepare)
    sent = Loop(store, Router())._history(sid, turn_id=tid)
    note = next(m.content for m in sent if m.content.startswith(memory_delivery.NOTE_PREFIX))
    assert "had not been acknowledged" in note
    saved = turn_context.load(store, tid)
    assert note in [m["content"] for m in saved["prefix"]]
    assert projected(store, item)["state"] == "saved"


@pytest.mark.parametrize(
    "text", ["How are you?", "My name is Mira", '"Remember that my name is Mira"']
)
def test_ordinary_or_quoted_input_adds_no_delivery_prompt(store, text):
    _, tid, _ = prepared(store, text=text)
    assert memory_delivery.prompt_note(store, tid) is None


def test_note_detection_normalizes_unicode_without_rewriting_source(store):
    text = "\uff32\uff45\uff4d\uff45\uff4d\uff42\uff45\uff52 that my name is Synthetic Mira"
    _, tid, item = prepared(store, text=text)
    assert memory_delivery.prompt_note(store, tid).startswith(memory_delivery.NOTE_PREFIX)
    assert store.get_message(item["id"])["content"] == text


def test_deeply_nested_transport_ack_is_rejected_without_worker_crash(store):
    item = message(store)
    with closing(
        Memory(
            store,
            MemoryClient(
                "http://memory.test",
                "ingest",
                "read",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content="[" * 1100 + "0" + "]" * 1100)
                ),
            ),
        )
    ) as memory:
        assert memory.drain_once() == 0
    assert projected(store, item)["state"] == "unavailable"
