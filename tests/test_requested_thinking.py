"""Explicit analysis survives admission/replay while cancellation stops its resources."""

import hashlib
import json
import threading
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_background_thinking import setup, wait_workers

from pi.agents import profiles as agents
from pi.api import server as api
from pi.conversation import retries as response_retries
from pi.conversation import settings as session_settings
from pi.conversation import submissions
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.turns import control as turn_control
from pi.turns import inference_control
from pi.turns import queue as turn_queue
from pi.turns import thinking as background_thinking


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "thinking.db")) as value:
        yield value


def setup_held(store):
    """`setup` with the longest thinking budget a role allows.

    These tests hold the thinker open across foreground work and then check how it
    ended. Its deadline is a wall-clock timer: on a loaded machine the fixture's
    10 s ran out first and the thinker ended at its time limit instead.
    """
    loop, provider = setup(store)
    saved = model_roles.load(store)
    saved["configuration"]["roleSettings"]["roles"]["thinking"]["timeoutMs"] = 120000
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=saved["revision"], configuration=saved["configuration"]
        ),
    )
    return loop, provider


def test_default_and_explicit_false_do_not_start_thinking(store):
    loop, provider = setup(store)
    sid = store.create_session()
    for value in ({}, {"think": False}):
        result = loop.run_turn(sid, "Ordinary conversation", **value)
        assert session_settings.execution(store, sid, result["turn_id"])["think"] is False
    assert background_thinking.list_for_session(store, sid) == []
    assert not provider.started.is_set()


def test_requested_thinking_is_frozen_and_replay_cannot_toggle_it(store, monkeypatch):
    loop, provider = setup(store)
    sid = store.create_session()
    bound = loop._run_bound

    def update_before_execution(*args, **kwargs):
        saved = model_roles.load(store)
        saved["configuration"]["roleSettings"]["roles"]["thinking"]["enabled"] = False
        model_roles.save(
            store,
            model_roles.Update(
                expected_revision=saved["revision"], configuration=saved["configuration"]
            ),
        )
        return bound(*args, **kwargs)

    monkeypatch.setattr(loop, "_run_bound", update_before_execution)
    result = loop.run_turn(sid, "Analyze", request_id="frozen_thinking_request", think=True)
    assert provider.started.wait(30)
    execution = session_settings.execution(store, sid, result["turn_id"])
    assert execution["think"] is True
    assert execution["modelConfiguration"]["roleSettings"]["roles"]["thinking"]["enabled"]
    assert submissions.get(store, "frozen_thinking_request")["think"] is True
    replay = loop.run_turn(sid, "Analyze", request_id="frozen_thinking_request", think=True)
    assert replay["replayed"] and replay["turn_id"] == result["turn_id"]
    with pytest.raises(submissions.SubmissionError, match="already used"):
        loop.run_turn(sid, "Analyze", request_id="frozen_thinking_request", think=False)
    wait_workers(loop, provider)


@pytest.mark.parametrize("value", [1, "true", None])
def test_non_boolean_request_rejected_before_admission(store, value):
    loop, _ = setup(store)
    sid = store.create_session()
    with pytest.raises(agents.AgentError, match="true or false"):
        loop.run_turn(sid, "Analyze", request_id="invalid_thinking_request", think=value)
    assert store.messages(sid) == []


def test_disabled_and_no_harness_reject_opt_in(store):
    loop, _ = setup(store)
    sid = store.create_session(privacy={"harnessDisabled": True})
    with pytest.raises(agents.AgentError, match="No-harness"):
        loop.run_turn(sid, "Analyze", think=True)
    assert store.messages(sid) == []
    saved = model_roles.load(store)
    saved["configuration"]["roleSettings"]["roles"]["thinking"]["enabled"] = False
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=saved["revision"], configuration=saved["configuration"]
        ),
    )
    sid = store.create_session()
    with pytest.raises(agents.AgentError, match="thinking model"):
        loop.run_turn(sid, "Analyze", think=True)
    assert store.messages(sid) == []


def test_legacy_receipt_defaults_false_and_false_keeps_legacy_fingerprint(store, monkeypatch):
    loop, _ = setup(store)
    sid = store.create_session()

    # Simulate the old admission writer, which stored no think member.
    def legacy_reserve(db, request_id, identity, *_):
        db.execute(
            "INSERT INTO submission_settings VALUES (?,?)",
            (request_id, json.dumps(session_settings._snapshot(db, identity))),
        )

    monkeypatch.setattr(session_settings, "reserve", legacy_reserve)
    first = loop.run_turn(sid, "Hello", request_id="legacy_thinking_request")
    assert first["submission"]["think"] is False
    replay = loop.run_turn(sid, "Hello", request_id="legacy_thinking_request", think=False)
    assert replay["replayed"]


def test_thinking_status_is_authorized_and_does_not_read_transcript(store, monkeypatch):
    loop, provider = setup(store)
    sid = store.create_session()
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "loop", loop, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "unused_admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(b"owner").hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime").hexdigest(), raising=False
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Gateway-Key": "runtime"}
    assert client.get(f"/sessions/{sid}/thinking").status_code == 401
    assert (
        client.post(
            f"/sessions/{sid}/turns", headers=headers, json={"text": "Analyze", "think": "true"}
        ).status_code
        == 422
    )
    result = client.post(
        f"/sessions/{sid}/turns", headers=headers, json={"text": "Analyze", "think": True}
    )
    assert result.status_code == 200, result.text
    assert provider.started.wait(30)
    monkeypatch.setattr(store, "messages", lambda *_: pytest.fail("status read transcript"))
    status = client.get(f"/sessions/{sid}/thinking", headers=headers)
    assert status.json()["schemaVersion"] == 1
    assert status.json()["session_id"] == sid
    assert status.json()["results"][0]["turn_id"] == result.json()["turn_id"]
    assert client.get("/sessions/missing/thinking", headers=headers).status_code == 404
    wait_workers(loop, provider)


class Stoppable:
    name = "one"

    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()

    def complete_bounded(self, messages, *, model, timeout):
        from pi.models.providers import Completion

        if model == "think":
            token = inference_control.current()
            with token.register(self.stopped.set):
                self.started.set()
                assert self.stopped.wait(30), "inference was not canceled"
                token.check()
        return Completion("Talking", model, self.name)


def test_cancel_closes_actual_inference_and_releases_slot_without_publishing(store):
    loop, _ = setup_held(store)
    provider = Stoppable()
    loop.router.local = provider
    sid = store.create_session()
    result = loop.run_turn(sid, "Analyze", think=True)
    assert provider.started.wait(30)
    background_thinking.cancel(store, sid, result["turn_id"])
    assert provider.stopped.wait(30)
    with loop.thinking.lock:
        threads = list(loop.thinking.threads)
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    row = background_thinking.list_for_session(store, sid)[0]
    assert row["status"] == "canceled" and row["result"] is None
    assert loop.thinking.slots.acquire(blocking=False)
    assert loop.thinking.slots.acquire(blocking=False)
    loop.thinking.slots.release()
    loop.thinking.slots.release()


def test_queue_freezes_think_and_rejects_changed_admission(store):
    setup(store)
    sid = store.create_session()
    item = turn_queue.enqueue(
        store,
        sid,
        turn_queue.Enqueue(request_id="queued_thinking_request", text="Analyze", think=True),
    )
    assert item["payload"]["think"] is True
    identity = turn_queue.submission_identity(item["id"], item["revision"])
    with pytest.raises(Exception, match="queued message"):
        submissions.reserve(
            store,
            identity,
            sid,
            "Analyze",
            {},
            queued_entry=(item["id"], item["revision"]),
            think=False,
        )
    receipt, created = submissions.reserve(
        store, identity, sid, "Analyze", {}, queued_entry=(item["id"], item["revision"]), think=True
    )
    assert created and receipt["think"] is True


def test_queued_cancel_and_restart_stop_owned_workers_without_replaying(store):
    loop, _ = setup_held(store)
    provider = Stoppable()
    loop.router.local = provider
    loop.thinking.slots = threading.BoundedSemaphore(1)
    first = store.create_session()
    loop.run_turn(first, "Running analysis", think=True)
    assert provider.started.wait(30)
    second = store.create_session()
    queued = loop.run_turn(second, "Queued analysis", think=True)
    assert background_thinking.list_for_session(store, second)[0]["status"] == "queued"
    background_thinking.cancel(store, second, queued["turn_id"])
    background_thinking.recover_interrupted(store)
    with loop.thinking.lock:
        workers = list(loop.thinking.threads)
    for worker in workers:
        worker.join(30)
        assert not worker.is_alive()
    assert background_thinking.list_for_session(store, first)[0]["status"] == "interrupted"
    assert background_thinking.list_for_session(store, second)[0]["status"] == "canceled"
    assert all(
        row["result"] is None
        for sid in (first, second)
        for row in background_thinking.list_for_session(store, sid)
    )


def test_stopping_running_submission_also_stops_its_thinker(store):
    loop, _ = setup_held(store)
    provider = Stoppable()
    original = provider.complete_bounded
    talking, release_talking = threading.Event(), threading.Event()

    def complete(messages, *, model, timeout):
        if model != "think":
            talking.set()
            assert release_talking.wait(30)
        return original(messages, model=model, timeout=timeout)

    provider.complete_bounded = complete
    loop.router.local = provider
    sid = store.create_session()
    errors = []

    def run():
        try:
            loop.run_turn(sid, "Analyze", request_id="stop_thinking_submission", think=True)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert talking.wait(30) and provider.started.wait(30)
    turn_control.cancel_submission(store, "stop_thinking_submission")
    assert provider.stopped.wait(30)
    release_talking.set()
    thread.join(30)
    assert not thread.is_alive() and errors
    row = background_thinking.list_for_session(store, sid)[0]
    assert row["status"] == "canceled" and row["result"] is None
    assert not any(message["role"] == "assistant" for message in store.messages(sid))


def test_failed_thinker_does_not_fail_talking_or_expose_provider_error(store):
    loop, provider = setup(store)
    original = provider.complete_bounded

    def complete(messages, *, model, timeout):
        if model == "think":
            raise RuntimeError("private-provider-token")
        return original(messages, model=model, timeout=timeout)

    provider.complete_bounded = complete
    sid = store.create_session()
    result = loop.run_turn(sid, "Analyze", think=True)
    assert result["message"]["content"] == "Talking reply"
    wait_workers(loop, provider)
    row = background_thinking.list_for_session(store, sid)[0]
    assert row["status"] == "failed" and row["result"] is None
    assert "private-provider-token" not in json.dumps(row)


def test_retrying_final_answer_does_not_restart_frozen_thinking_request(store):
    loop, provider = setup(store)
    sid = store.create_session()
    result = loop.run_turn(sid, "Analyze", think=True)
    wait_workers(loop, provider)
    repeated = response_retries.run(
        loop,
        sid,
        result["message"]["id"],
        response_retries.Retry(request_id="thinking_reply_only_retry", model_id="a"),
    )
    assert repeated["status"] == "complete"
    rows = background_thinking.list_for_session(store, sid)
    assert len(rows) == 1 and rows[0]["turn_id"] == result["turn_id"]


def test_ordinary_followup_keeps_requested_findings_anchored_to_original_input(store):
    loop, provider = setup_held(store)
    sid = store.create_session()
    original = loop.run_turn(sid, "Analyze this original question", think=True)
    assert provider.started.wait(30)
    loop.run_turn(sid, "Keep talking while that runs")
    assert background_thinking.list_for_session(store, sid)[0]["status"] == "running"
    wait_workers(loop, provider)
    row = background_thinking.list_for_session(store, sid)[0]
    assert row["status"] == "completed" and row["turn_id"] == original["turn_id"]
    assert store.get_message(row["input_message_id"])["content"] == "Analyze this original question"
    assert not any("Keep talking" in message.content for message in provider.messages)


def test_worker_start_failure_leaves_talking_available(store, monkeypatch):
    loop, _ = setup(store)

    def unavailable(_):
        raise RuntimeError("private startup information")

    monkeypatch.setattr(background_thinking.threading.Thread, "start", unavailable)
    sid = store.create_session()
    result = loop.run_turn(sid, "Analyze", think=True)
    assert result["message"]["content"] == "Talking reply"
    row = background_thinking.list_for_session(store, sid)[0]
    assert row["status"] == "failed" and "private startup" not in row["detail"]


def test_shutdown_serializes_worker_admission_and_never_joins_unstarted_thread(store, monkeypatch):
    loop, _ = setup_held(store)
    loop.router.local = Stoppable()
    admitted, release, closing_started, closed = (threading.Event() for _ in range(4))
    constructor = background_thinking._Control
    errors = []

    def delayed_control(session_id):
        admitted.set()
        assert release.wait(30)
        return constructor(session_id)

    monkeypatch.setattr(background_thinking, "_Control", delayed_control)
    sid = store.create_session()

    def run():
        try:
            loop.run_turn(sid, "Analyze", think=True)
        except Exception as exc:
            errors.append(exc)

    def close():
        closing_started.set()
        try:
            loop.thinking.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            closed.set()

    request = threading.Thread(target=run, daemon=True)
    shutdown = threading.Thread(target=close, daemon=True)
    request.start()
    assert admitted.wait(30)
    shutdown.start()
    assert closing_started.wait(30)
    assert not closed.wait(0.05)
    release.set()
    request.join(30)
    shutdown.join(30)
    assert not request.is_alive() and not shutdown.is_alive() and not errors
    assert background_thinking.list_for_session(store, sid)[0]["status"] == "interrupted"
    # close() waits only 1 s per worker; a loaded machine can take longer to unwind one.
    with loop.thinking.lock:
        workers = list(loop.thinking.threads)
    for worker in workers:
        worker.join(30)
        assert not worker.is_alive()
    assert loop.thinking.closed and not loop.thinking.threads
