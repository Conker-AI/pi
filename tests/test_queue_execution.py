"""Real submission admission is atomic with FIFO queue ownership."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from test_loop import Recorder, loop_with
from test_turn_queue import body

from pi import session_settings, submissions, tasks
from pi import turn_queue as q
from pi.queue_worker import QueueWorker
from pi.store import Store


def test_worker_drains_in_order_once_with_shared_history(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        q.enqueue(store, sid, body(0, "first"))
        q.enqueue(store, sid, body(1, "second"))
        provider = Recorder()
        worker = QueueWorker(store, loop_with(store, provider))
        worker.tick()
        assert len(q.read(store, sid)["entries"]) == 1
        worker.tick()
        worker.tick()
        assert q.read(store, sid)["entries"] == []
        assert len(provider.calls) == 2
        assert [m["content"] for m in store.messages(sid) if m["role"] == "user"] == [
            "first",
            "second",
        ]


def test_concurrent_consumers_cannot_duplicate_provider_request(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        q.enqueue(store, sid, body())
        started, release = threading.Event(), threading.Event()

        class Blocking(Recorder):
            def complete(self, messages, *, model):
                started.set()
                assert release.wait(5)
                return super().complete(messages, model=model)

        provider = Blocking()
        loop = loop_with(store, provider)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(q.run_next, store, loop, sid)
            assert started.wait(5)
            try:
                waiting = q.run_next(store, loop, sid)
                assert waiting["entries"][0]["state"] == "claimed"
            finally:
                release.set()
            assert running.result(5)["entries"] == []
        assert len(provider.calls) == 1


def test_admission_rechecks_changed_settings_and_pause(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        entry = q.enqueue(store, sid, body())
        loop = loop_with(store, Recorder())
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=True),
                ),
            ),
        )
        with pytest.raises(tasks.TaskError, match="review"):
            loop.run_turn(
                sid,
                "Next question",
                request_id=q.submission_identity(entry["id"], 1),
                queued_entry=(entry["id"], 1),
            )
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM turn_submissions").fetchone()[0] == 0
        assert q.run_next(store, loop, sid)["paused"]


def test_failed_provider_retains_entry_without_automatic_retry(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        q.enqueue(store, sid, body())
        q.enqueue(store, sid, body(1))
        provider = Recorder(fail=True)
        worker = QueueWorker(store, loop_with(store, provider))
        worker.tick()
        worker.tick()
        value = q.read(store, sid)
        assert value["paused"] and value["entries"][0]["state"] == "failed"
        assert len(provider.calls) == 1
        with pytest.raises(tasks.TaskError, match="Remove failed"):
            q.pause(store, sid, q.QueueRevision(expected_revision=value["revision"]), False)
        q.change(store, sid, body().request_id, q.Revision(expected_revision=1), "remove")
        value = q.read(store, sid)
        q.pause(store, sid, q.QueueRevision(expected_revision=value["revision"]), False)
        assert len(q.read(store, sid)["entries"]) == 1


def test_restart_reconciles_reserved_request_without_dispatch(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        entry = q.enqueue(store, sid, body())
        request = q.submission_identity(entry["id"], 1)
        submissions.reserve(store, request, sid, "Next question", {}, queued_entry=(entry["id"], 1))
    with closing(Store(path)) as store:
        store.mark_interrupted_turns()
        provider = Recorder()
        value = q.run_next(store, loop_with(store, provider), sid)
        assert value["paused"] and value["entries"][0]["state"] == "failed"
        assert provider.calls == []


def test_ordinary_active_turn_is_waited_for_not_steered(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        turn = store.start_turn(sid)
        q.enqueue(store, sid, body())
        provider = Recorder()
        loop = loop_with(store, provider)
        value = q.run_next(store, loop, sid)
        assert not value["paused"] and value["entries"][0]["state"] == "waiting"
        assert provider.calls == []
        store.complete_turn(turn, "previous reply")
        assert q.run_next(store, loop, sid)["entries"] == []
        assert len(provider.calls) == 1


def test_restart_after_completion_reconciles_without_second_provider_call(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        entry = q.enqueue(store, sid, body())
        loop_with(store, Recorder()).run_turn(
            sid,
            "Next question",
            request_id=q.submission_identity(entry["id"], 1),
            queued_entry=(entry["id"], 1),
        )
        assert q.read(store, sid)["entries"][0]["state"] == "claimed"
    with closing(Store(path)) as store:
        provider = Recorder()
        assert q.run_next(store, loop_with(store, provider), sid)["entries"] == []
        assert provider.calls == []


def test_approval_hold_pauses_following_messages(tmp_path):
    from test_tool_turns import CALL, FakeGate, build

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        q.enqueue(store, sid, body())
        q.enqueue(store, sid, body(1, "Should wait"))
        gate = FakeGate(needs_approval=True)
        loop, provider = build(store, [CALL, "Must not answer next message"], gate)
        worker = QueueWorker(store, loop)
        worker.tick()
        worker.tick()
        value = q.read(store, sid)
        assert value["paused"] and value["reason"] == "turn_needs_review"
        assert len(gate.invocations) == len(provider.sent) == 1
        assert [entry["state"] for entry in value["entries"]] == ["claimed", "waiting"]


def test_queued_attachment_reaches_actual_turn_input(tmp_path):
    from test_attachment_turns import upload

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        identity = upload(store, sid)
        q.enqueue(store, sid, body(attachment_ids=[identity]))
        provider = Recorder()
        assert q.run_next(store, loop_with(store, provider), sid)["entries"] == []
        assert store.messages(sid)[0]["attachments"][0]["id"] == identity


def test_worker_rotates_past_busy_conversations(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sessions = sorted(store.create_session() for _ in range(21))
        for index, sid in enumerate(sessions):
            q.enqueue(store, sid, body(index))
            if index < 20:
                store.start_turn(sid)
        provider = Recorder()
        worker = QueueWorker(store, loop_with(store, provider))
        worker.tick()
        assert provider.calls == []
        worker.tick()
        assert len(provider.calls) == 1
        assert q.read(store, sessions[-1])["entries"] == []
