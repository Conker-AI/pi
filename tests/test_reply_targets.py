"""Reply references are exact context references, not permission or policy overrides."""

from contextlib import closing

import pytest
from test_context_controls import policy
from test_loop import Recorder, loop_with

from pi import agents, submissions, tasks
from pi import context_controls as c
from pi import turn_queue as q
from pi.loop import TurnFailed
from pi.store import Store


def test_reply_is_present_in_model_context_and_input_projection_after_reopen(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        target = store.append_message(sid, "assistant", "The earlier answer")
        provider = Recorder()
        loop = loop_with(store, provider)
        result = loop.run_turn(
            sid, "Explain this", request_id="reply_target_0001", reply_to=target["id"]
        )
        sent = [m.content for m in provider.calls[0]]
        assert any(target["id"] in text and "reply target" in text for text in sent)
        assert "The earlier answer" in sent
        input_id = result["submission"]["input_message_id"]
        assert store.get_message(input_id)["reply_to"] == target["id"]
        assert loop.run_turn(
            sid, "Explain this", request_id="reply_target_0001", reply_to=target["id"]
        )["replayed"]
        with pytest.raises(submissions.SubmissionError):
            loop.run_turn(sid, "Explain this", request_id="reply_target_0001")
        assert len(provider.calls) == 1
    with closing(Store(path)) as store:
        assert store.get_message(input_id)["reply_to"] == target["id"]


def test_foreign_and_excluded_targets_do_not_admit_or_dispatch(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        foreign = store.append_message(store.create_session(), "user", "Foreign")
        own = store.append_message(sid, "assistant", "Excluded")
        c.save(store, sid, c.Update(expected_revision=0, policy=policy({own["id"]: "exclude"})))
        provider = Recorder()
        loop = loop_with(store, provider)
        for target in (foreign["id"], own["id"]):
            with pytest.raises(agents.AgentError):
                loop.run_turn(sid, "reply", request_id="invalid_reply_001", reply_to=target)
        assert provider.calls == []
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM turn_submissions").fetchone()[0] == 0


def test_queue_review_can_clear_unavailable_reply_target(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        target = store.append_message(sid, "assistant", "Earlier")
        entry = q.enqueue(
            store,
            sid,
            q.Enqueue(request_id="queue_reply_0001", text="Explain", reply_to=target["id"]),
        )
        c.save(store, sid, c.Update(expected_revision=0, policy=policy({target["id"]: "exclude"})))
        assert q.read(store, sid)["paused"]
        with pytest.raises(agents.AgentError):
            q.change(store, sid, entry["id"], q.Review(expected_revision=1), "review")
        q.change(store, sid, entry["id"], q.Review(expected_revision=1, reply_to=None), "review")
        current = q.read(store, sid)
        q.pause(store, sid, q.QueueRevision(expected_revision=current["revision"]), False)
        provider = Recorder()
        assert q.run_next(store, loop_with(store, provider), sid)["entries"] == []
        assert all(m.content != "Earlier" for m in provider.calls[0])


def test_reply_target_cannot_be_silently_summarized(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        target = store.append_message(sid, "assistant", "Long answer" * 20)
        provider = Recorder()
        loop = loop_with(store, provider, fork_threshold_chars=100)
        with pytest.raises(TurnFailed, match="selected target"):
            loop.run_turn(sid, "Explain", request_id="reply_fork_00001", reply_to=target["id"])
        assert provider.calls == []
        assert store.get_session(sid)["status"] == "open"


def test_queue_admission_rejects_substituted_target(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        first = store.append_message(sid, "assistant", "First")
        second = store.append_message(sid, "assistant", "Second")
        entry = q.enqueue(
            store,
            sid,
            q.Enqueue(request_id="queue_reply_0001", text="Explain", reply_to=first["id"]),
        )
        with pytest.raises(tasks.TaskError, match="match"):
            loop_with(store, Recorder()).run_turn(
                sid,
                "Explain",
                reply_to=second["id"],
                request_id=q.submission_identity(entry["id"], 1),
                queued_entry=(entry["id"], 1),
            )
