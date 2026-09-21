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
