"""Requests reconcile by identity; crashes never authorize duplicate model/tool execution."""

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_loop import Recorder, loop_with
from test_tool_turns import CALL, FakeGate, build

from pi import activity, api, forgetting, submissions, tasks
from pi.loop import ActedWithoutReply, TurnFailed
from pi.providers import ProviderUnavailable
from pi.store import Store
from pi.toolgate import ToolPending

REQUEST = "submission_request_001"


class Crash(BaseException):
    pass


@pytest.fixture()
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_replay_uses_current_receipt_without_repeating_model_and_survives_reopen(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        provider = Recorder()
        loop = loop_with(store, provider)
        session = store.create_session()
        result = loop.run_turn(session, "hello", request_id=REQUEST)
        replay = loop.run_turn(session, "hello", request_id=REQUEST)
        assert len(provider.calls) == 1 and len(store.messages(session)) == 2
        assert replay["replayed"] is True and result["replayed"] is False
        assert replay["message"] == result["message"]
        assert replay["submission"]["input_message_id"] == store.messages(session)[0]["id"]
        assert [ref["purpose"] for ref in replay["submission"]["message_refs"]] == (
            ["input", "final"]
        )
        assert (
            activity.get_run(store, result["turn_id"])["message_refs"]
            == (replay["submission"]["message_refs"])
        )
        with pytest.raises(submissions.SubmissionError, match="already used"):
            loop.run_turn(session, "different words", request_id=REQUEST)
    with closing(Store(path)) as store:
        unavailable = Recorder(fail=True)
        replay = loop_with(store, unavailable).run_turn(session, "hello", request_id=REQUEST)
        assert replay["turn_id"] == result["turn_id"] and unavailable.calls == []


def test_reservation_precedes_summarizer_and_concurrent_duplicate_does_not_fork_twice(store):
    started, release = threading.Event(), threading.Event()

    class Blocking(Recorder):
        def complete(self, messages, *, model):
            if not self.calls:
                started.set()
                assert release.wait(5)
            return super().complete(messages, model=model)

    provider = Blocking("short answer")
    loop = loop_with(store, provider, fork_threshold_chars=100)
    session = store.create_session()
    store.append_message(session, "user", "x" * 120)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(loop.run_turn, session, "hello", request_id=REQUEST)
        assert started.wait(5)
        try:
            waiting = submissions.get(store, REQUEST)
            assert waiting["state"] == "preparing" and waiting["pending_text"] == "hello"
            duplicate = loop.run_turn(session, "hello", request_id=REQUEST)
            assert duplicate["status"] == "preparing"
            with pytest.raises(submissions.SubmissionError) as error:
                loop.run_turn(session, "other request", request_id="submission_request_002")
            assert error.value.detail["code"] == "session_busy"
        finally:
            release.set()
        result = first.result(5)
    child = result["session_id"]
    assert child != session and len(store.list_sessions()) == 2
    assert len(provider.calls) == 2  # One summary and one actual response.
    assert store.get_session(session)["status"] == "forked"
    assert result["submission"]["requested_session_id"] == session
    assert result["submission"]["effective_session_id"] == child
    assert loop.run_turn(session, "hello", request_id=REQUEST)["session_id"] == child
    assert len(provider.calls) == 2


def test_crash_during_preparation_retains_input_and_never_automatically_reexecutes(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        store.append_message(session, "user", "x" * 200)
        loop = loop_with(store, Recorder(), fork_threshold_chars=100)
        loop._summarise = lambda _, execution=None: (_ for _ in ()).throw(Crash())
        with pytest.raises(Crash):
            loop.run_turn(session, "still saved", request_id=REQUEST)
        assert submissions.get(store, REQUEST)["pending_text"] == "still saved"
        assert store.get_session(session)["status"] == "open" and store.turns(session) == []
    with closing(Store(path)) as store:
        assert store.mark_interrupted_turns() == 1
        provider = Recorder()
        replay = loop_with(store, provider).run_turn(session, "still saved", request_id=REQUEST)
        assert replay["status"] == "preparation_interrupted" and provider.calls == []
        assert replay["submission"]["pending_text"] == "still saved"
        assert store.mark_interrupted_turns() == 0


def test_fork_binding_failure_rolls_back_parent_child_turn_input_and_outbox(store):
    session = store.create_session()
    store.append_message(session, "user", "x" * 200)
    loop = loop_with(store, Recorder(), fork_threshold_chars=100)
    with store._connect() as db:
        db.executescript(
            "CREATE TRIGGER fail_bind BEFORE UPDATE OF turn_id ON turn_submissions "
            "BEGIN SELECT RAISE(ABORT,'bind failed'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError, match="bind failed"):
        loop.run_turn(session, "hello", request_id=REQUEST)
    assert len(store.list_sessions()) == 1 and store.get_session(session)["status"] == "open"
    assert store.turns(session) == [] and len(store.messages(session)) == 1
    receipt = submissions.get(store, REQUEST)
    assert receipt["state"] == "preparation_failed" and receipt["pending_text"] == "hello"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 1


def test_task_link_is_atomic_revision_checked_and_does_not_claim_completion(store):
    session = store.create_session()
    task = tasks.create(
        store,
        tasks.CreateTask(
            request_id="task_request_identity",
            session_id=session,
            outcome="Write an answer",
            criteria=["Owner reviewed it"],
        ),
    )
    provider = Recorder()
    loop = loop_with(store, provider)
    result = loop.run_turn(
        session, "answer", request_id=REQUEST, task_id=task["id"], task_expected_revision=1
    )
    saved = tasks.get(store, task["id"])
    assert saved["run_ids"] == [result["turn_id"]]
    assert saved["revision"] == 2 and saved["status"] == "planned"
    replay = loop.run_turn(
        session, "answer", request_id=REQUEST, task_id=task["id"], task_expected_revision=1
    )
    assert replay["turn_id"] == result["turn_id"] and len(provider.calls) == 1
    with pytest.raises(tasks.TaskError) as error:
        loop.run_turn(
            session,
            "again",
            request_id="submission_request_002",
            task_id=task["id"],
            task_expected_revision=1,
        )
    assert error.value.detail["code"] == "revision_conflict"


def test_task_bound_autofork_rejects_before_model_or_tool_call(store):
    session = store.create_session()
    task = tasks.create(
        store,
        tasks.CreateTask(
            request_id="task_request_identity",
            session_id=session,
            outcome="Write an answer",
            criteria=["Owner reviewed it"],
        ),
    )
    store.append_message(session, "user", "x" * 200)
    provider = Recorder()
    loop = loop_with(store, provider, fork_threshold_chars=100)
    with pytest.raises(submissions.SubmissionError) as error:
        loop.run_turn(
            session, "answer", request_id=REQUEST, task_id=task["id"], task_expected_revision=1
        )
    assert error.value.detail["code"] == "task_fork_required" and provider.calls == []
    assert len(store.list_sessions()) == 1 and store.turns(session) == []
    assert tasks.get(store, task["id"])["revision"] == 1
    assert submissions.get(store, REQUEST)["pending_text"] == "answer"


def test_failed_provider_replay_does_not_append_or_generate_again(store):
    provider = Recorder(fail=True)
    loop = loop_with(store, provider)
    session = store.create_session()
    with pytest.raises(TurnFailed):
        loop.run_turn(session, "hello", request_id=REQUEST)
    calls = len(provider.calls)
    replay = loop.run_turn(session, "hello", request_id=REQUEST)
    assert replay["status"] == "failed" and replay["message"] is None
    assert len(provider.calls) == calls and len(store.messages(session)) == 1


def test_dispatch_crash_reconciles_original_action_without_new_invoke(store):
    class Gate(FakeGate):
        def invoke(self, *args, **kwargs):
            self.saved_action = kwargs["action_id"]
            super().invoke(*args, **kwargs)
            raise Crash()

        def check_action(self, action_id, tool_id):
            assert action_id == self.saved_action
            return ToolPending("outcome_unknown", "Still uncertain", action_id)

    gate = Gate()
    loop, _ = build(store, [CALL, "done"], gate)
    session = store.create_session()
    with pytest.raises(Crash):
        loop.run_turn(session, "send", request_id=REQUEST)
    store.mark_interrupted_turns()
    replay = loop.run_turn(session, "send", request_id=REQUEST)
    assert replay["status"] == "outcome_unknown"
    assert loop.resume_turn(replay["turn_id"])["status"] == "outcome_unknown"
    assert len(gate.invocations) == 1


def test_crash_after_effect_record_keeps_associated_result_and_resume_only_adds_reply(store):
    gate = FakeGate()
    loop, _ = build(store, [CALL, "done"], gate)
    session = store.create_session()
    store.mark_acted = lambda _: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        loop.run_turn(session, "send", request_id=REQUEST)
    store.mark_interrupted_turns()
    replay = loop.run_turn(session, "send", request_id=REQUEST)
    assert replay["status"] == "acted_no_reply"
    assert [ref["purpose"] for ref in replay["submission"]["message_refs"]] == (
        ["input", "intermediate", "tool_result"]
    )
    loop.resume_turn(replay["turn_id"])
    assert len(gate.invocations) == 1
    final = submissions.get(store, REQUEST)
    assert final["status"] == "complete" and final["final_message_id"]
    assert [ref["purpose"] for ref in final["message_refs"]].count("final") == 1


def test_approval_and_reply_failure_keep_same_associations_through_resume(store):
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL, ProviderUnavailable("failed reply"), "done"], gate)
    session = store.create_session()
    parked = loop.run_turn(session, "send", request_id=REQUEST)
    assert parked["submission"]["status"] == "awaiting_approval"
    assert loop.run_turn(session, "send", request_id=REQUEST)["status"] == "awaiting_approval"
    assert len(gate.invocations) == 1
    with pytest.raises(ActedWithoutReply):
        loop.resume_turn(parked["turn_id"])
    loop.resume_turn(parked["turn_id"])
    assert len(gate.invocations) == 2  # Initial approval request + one approved dispatch.
    refs = submissions.get(store, REQUEST)["message_refs"]
    assert [ref["purpose"] for ref in refs] == ["input", "intermediate", "tool_result", "final"]


def test_final_message_and_turn_completion_are_one_commit(store):
    session = store.create_session()
    with store._connect() as db:
        db.executescript(
            "CREATE TRIGGER fail_final BEFORE UPDATE OF status ON turns "
            "WHEN NEW.status='complete' "
            "BEGIN SELECT RAISE(ABORT,'completion failed'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError, match="completion failed"):
        loop_with(store, Recorder()).run_turn(session, "hello", request_id=REQUEST)
    receipt = submissions.get(store, REQUEST)
    assert receipt["status"] == "running" and receipt["final_message_id"] is None
    assert [message["role"] for message in store.messages(session)] == ["user"]
    assert [ref["purpose"] for ref in receipt["message_refs"]] == ["input"]


def test_associations_are_immutable_and_reject_foreign_or_legacy_guesses(store):
    session = store.create_session()
    result = loop_with(store, Recorder()).run_turn(session, "hello", request_id=REQUEST)
    message_id = result["message"]["id"]
    foreign = store.create_session()
    run = store.start_turn(foreign)
    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE turn_messages SET turn_id=? WHERE message_id=?", (run, message_id))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM turn_messages WHERE message_id=?", (message_id,))
    before = len(store.messages(session))
    with pytest.raises(sqlite3.IntegrityError, match="association"):
        store.append_message(session, "assistant", "incorrect", turn_id=run, purpose="final")
    assert len(store.messages(session)) == before
    legacy = store.start_turn(store.create_session())
    assert activity.get_run(store, legacy)["message_refs"] == []


def test_forgetting_clears_pending_and_bound_payload_hashes_and_preserves_tombstones(tmp_path):
    path = tmp_path / "pi.db"
    secret = "submission-secret-6994-не-помни"
    with closing(Store(path)) as store:
        parent = store.create_session()
        child = store.create_session(parent_id=parent)
        loop = loop_with(store, Recorder("safe reply"))
        loop.run_turn(parent, secret, request_id=REQUEST)
        submissions.reserve(store, "submission_request_002", child, secret, {})
        with store._connect() as db:
            hashes = [row[0] for row in db.execute("SELECT payload_hash FROM turn_submissions")]
    preview = forgetting.preview(path, parent)
    assert len(preview["submission_ids"]) == 2
    forgetting.forget(path, parent, preview["confirmation"])
    with closing(Store(path)) as store:
        for identity in (REQUEST, "submission_request_002"):
            receipt = submissions.get(store, identity)
            assert receipt["content_status"] == "forgotten" and receipt["pending_text"] is None
            assert secret not in json.dumps(receipt)
        with pytest.raises(submissions.SubmissionError, match="already used"):
            submissions.reserve(store, REQUEST, parent, secret, {})
    for entry in path.parent.glob("pi.db*"):
        content = entry.read_bytes()
        assert secret.encode() not in content
        assert all(digest.encode() not in content for digest in hashes)


def test_new_pending_submission_invalidates_forgetting_preview(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
    first = forgetting.preview(path, session)
    with closing(Store(path)) as store:
        submissions.reserve(store, REQUEST, session, "not yet in messages", {})
    with pytest.raises(forgetting.ForgettingError):
        forgetting.forget(path, session, first["confirmation"])


@pytest.mark.parametrize(
    "status",
    [
        "awaiting_approval",
        "awaiting_budget",
        "acted_no_reply",
        "action_in_progress",
        "outcome_unknown",
    ],
)
def test_new_submission_cannot_bypass_a_parked_unresolved_turn(store, status):
    session = store.create_session()
    turn = store.start_turn(session)
    store.finish_turn(turn, status)
    with pytest.raises(submissions.SubmissionError) as error:
        loop_with(store, Recorder()).run_turn(session, "new work", request_id=REQUEST)
    assert error.value.detail["code"] == "session_busy"
    assert store.messages(session) == []


def test_pending_references_are_paginated_without_exposing_input_or_digest(store):
    session = store.create_session()
    for index in range(3):
        identity = f"submission_request_00{index}"
        submissions.reserve(store, identity, session, "pending private words", {})
        submissions.fail_preparation(store, identity)
    ids, cursor = [], None
    while True:
        page = submissions.list_pending(store, session, limit=1, cursor=cursor)
        assert "pending private words" not in json.dumps(page)
        assert "payload_hash" not in json.dumps(page)
        ids.extend(row["request_id"] for row in page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(ids) == len(set(ids)) == 3


def test_replace_cannot_repoint_associations_or_reuse_submission_identity(store):
    session = store.create_session()
    receipt, _ = submissions.reserve(store, REQUEST, session, "hello", {})
    receipt = submissions.bind(store, REQUEST)
    message = store.append_message(session, "user", "other input")
    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "INSERT OR REPLACE INTO turn_messages VALUES(?,?,'input',NULL)",
                (message["id"], receipt["turn_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="permanent"):
            db.execute("DELETE FROM turn_submissions WHERE request_id=?", (REQUEST,))
        with pytest.raises(sqlite3.IntegrityError, match="fixed"):
            db.execute(
                "UPDATE turn_submissions SET pending_text='new text' WHERE request_id=?", (REQUEST,)
            )
    assert submissions.get(store, REQUEST)["input_message_id"] == receipt["input_message_id"]


def test_http_receipts_are_authenticated_and_legacy_submission_still_works(monkeypatch, store):
    key = "runtime_key_for_submissions"
    provider = Recorder()
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "loop", loop_with(store, provider), raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery_only", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(key.encode()).hexdigest(), raising=False
    )
    session = store.create_session()
    client = TestClient(api.app)
    try:
        assert client.get(f"/turn-submissions/{REQUEST}").status_code == 401
        headers = {"X-Pi-Gateway-Key": key}
        payload = {"text": "hello", "request_id": REQUEST}
        first = client.post(f"/sessions/{session}/turns", json=payload, headers=headers)
        second = client.post(f"/sessions/{session}/turns", json=payload, headers=headers)
        assert first.status_code == second.status_code == 200 and second.json()["replayed"]
        assert client.get(f"/turn-submissions/{REQUEST}", headers=headers).json()["status"] == (
            "complete"
        )
        assert len(provider.calls) == 1
        submissions.reserve(store, "pending_submission_002", session, "held private words", {})
        detail = client.get(f"/sessions/{session}", headers=headers).json()
        assert detail["pending_submissions"][0]["request_id"] == "pending_submission_002"
        assert "held private words" not in json.dumps(detail["pending_submissions"])
        page = client.get(f"/sessions/{session}/submissions", headers=headers).json()
        assert len(page["results"]) == 1
        submissions.fail_preparation(store, "pending_submission_002")
        legacy = client.post(f"/sessions/{session}/turns", json={"text": "legacy"}, headers=headers)
        assert legacy.status_code == 200 and legacy.json()["message"]
        invalid = client.post(
            f"/sessions/{session}/turns",
            headers=headers,
            json={"text": "task", "task_id": "t", "task_expected_revision": 1},
        )
        assert invalid.status_code == 422
    finally:
        client.close()
