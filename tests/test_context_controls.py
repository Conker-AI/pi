from contextlib import closing

import pytest

from pi import context_controls as c
from pi import forgetting
from pi.loop import Loop, TurnFailed
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


class Provider:
    name = "local"

    def __init__(self):
        self.calls = []

    def health(self):
        return {"status": "ok"}

    def complete(self, messages, *, model):
        self.calls.append(messages)
        return Completion(text="Answer", provider=self.name, model=model)


def policy(selectors=None, text="Be critical", window=10000):
    return c.Policy(
        sessionInstructions=text,
        messagePolicies=selectors or {},
        budget=c.Budget(contextWindowTokens=window, outputReserveTokens=10, otherInputTokens=0),
    )


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "test.db")) as db:
        yield db


def test_real_loop_excludes_history_and_keeps_instruction(store):
    sid = store.create_session()
    excluded = store.append_message(sid, "user", "Excluded secret")
    pinned = store.append_message(sid, "assistant", "Exact important detail")
    c.save(
        store,
        sid,
        c.Update(
            expected_revision=0,
            policy=policy({excluded["id"]: "exclude", pinned["id"]: "keep-exact"}),
        ),
    )
    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"))
    result = loop.run_turn(sid, "Next question")
    sent = [message.content for message in provider.calls[-1]]
    assert "Excluded secret" not in sent
    assert "Exact important detail" in sent and "Be critical" in sent
    assert store.get_message(excluded["id"])["content"] == "Excluded secret"
    frozen = c.load(store, sid, result["turn_id"])
    c.save(store, sid, c.Update(expected_revision=1, policy=policy(text="New instruction")))
    assert c.load(store, sid, result["turn_id"]) == frozen


def test_boundary_revision_retrieval_and_budget(store):
    sid, other = store.create_session(), store.create_session()
    foreign = store.append_message(other, "user", "Foreign")
    with pytest.raises(c.ContextError):
        c.save(
            store, sid, c.Update(expected_revision=0, policy=policy({foreign["id"]: "keep-exact"}))
        )
    own = store.append_message(sid, "user", "a" * 100)
    c.save(store, sid, c.Update(expected_revision=0, policy=policy({own["id"]: "retrieve"})))
    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"))
    with pytest.raises(c.ContextError):
        loop.run_turn(sid, "Question")
    assert not provider.calls
    with pytest.raises(c.ContextError):
        c.save(store, sid, c.Update(expected_revision=0, policy=policy()))
    c.save(store, sid, c.Update(expected_revision=1, policy=policy(window=20)))
    with pytest.raises(c.ContextError):
        loop.run_turn(sid, "Question")
    assert not provider.calls


def test_no_silent_auto_fork_or_pin_loss(store):
    sid = store.create_session()
    c.save(store, sid, c.Update(expected_revision=0, policy=policy()))
    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"), fork_threshold_chars=5)
    with pytest.raises(TurnFailed):
        loop.run_turn(sid, "Long question")
    assert not provider.calls
    assert store.get_session(sid)["status"] == "open"


def test_forgetting_scrubs_policy_and_snapshot(store):
    sid = store.create_session()
    c.save(store, sid, c.Update(expected_revision=0, policy=policy(text="PRIVATE_POLICY_TOKEN")))
    store.start_turn(sid)
    path = store.path
    store.close()
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    assert b"PRIVATE_POLICY_TOKEN" not in path.read_bytes()


def test_reviewed_fork_carries_original_pins_without_memory_duplication(store):
    sid = store.create_session()
    pin = store.append_message(sid, "user", "Keep these exact words")
    last = store.append_message(sid, "assistant", "Can be summarized")
    c.save(store, sid, c.Update(expected_revision=0, policy=policy({pin["id"]: "keep-exact"})))
    body = c.ReviewedFork(
        expected_revision=1,
        expected_last_message_id=last["id"],
        summary="Reviewed short summary",
        request_id="fork-request-0001",
    )
    result = c.reviewed_fork(store, sid, body)
    child = result["session_id"]
    assert c.reviewed_fork(store, sid, body)["session_id"] == child
    assert store.messages(child) == []
    assert c.history(store, child)[0]["id"] == pin["id"]
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 1
    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"))
    loop.run_turn(child, "Continue")
    sent = [message.content for message in provider.calls[-1]]
    assert "Keep these exact words" in sent and "Be critical" in sent
    assert "Can be summarized" not in sent
    assert any("Reviewed short summary" in text for text in sent)
    assert store.get_session(child)["parent_id"] == sid


def test_reviewed_fork_stale_boundary_and_active_turn_are_atomic(store):
    sid = store.create_session()
    c.save(store, sid, c.Update(expected_revision=0, policy=policy()))
    body = c.ReviewedFork(expected_revision=1, summary="Summary", request_id="fork-request-0002")
    store.append_message(sid, "user", "Arrived after review")
    with pytest.raises(c.ContextError, match="Conversation changed"):
        c.reviewed_fork(store, sid, body)
    assert store.get_session(sid)["status"] == "open"
    last = store.messages(sid)[-1]["id"]
    store.start_turn(sid)
    with pytest.raises(c.ContextError, match="Finish the current turn"):
        c.reviewed_fork(store, sid, body.model_copy(update={"expected_last_message_id": last}))
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_reviewed_fork_after_stop_but_not_during_preparation(store):
    from pi import submissions, turn_control

    sid = store.create_session()
    c.save(store, sid, c.Update(expected_revision=0, policy=policy()))
    body = c.ReviewedFork(expected_revision=1, summary="Reviewed", request_id="fork-after-stop-001")
    submissions.reserve(store, "fork-preparing-001", sid, "pending", {})
    with pytest.raises(c.ContextError, match="Finish"):
        c.reviewed_fork(store, sid, body)
    turn_control.cancel_submission(store, "fork-preparing-001")
    turn = store.start_turn(sid)
    turn_control.cancel(store, turn)
    store.finish_turn(turn, "failed")
    assert c.reviewed_fork(store, sid, body)["parent_id"] == sid
