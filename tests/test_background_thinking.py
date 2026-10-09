import threading
from contextlib import closing

from test_model_roles import config

from pi import background_thinking, model_roles
from pi.loop import Loop
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


class ConcurrentProvider:
    name = "one"

    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()
        self.messages = None

    def complete_bounded(self, messages, *, model, timeout):
        if model == "think":
            self.messages = messages
            self.started.set()
            assert self.release.wait(5)
            return Completion(text="Useful findings", model=model, provider=self.name)
        return Completion(text="Talking reply", model=model, provider=self.name)


def setup(store):
    value = config()
    value["models"].append(
        dict(id="thinker", providerId="one", name="Thinker", route="think", enabled=True)
    )
    value["roleSettings"]["roles"]["thinking"].update(
        enabled=True, eligibleModelIds=["thinker"], modelId="thinker"
    )
    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=value))
    provider = ConcurrentProvider()
    loop = Loop(store, Router(local_provider=provider, local_model="actual-a"))
    return loop, provider


def wait_workers(loop, provider):
    provider.release.set()
    with loop.thinking.lock:
        workers = list(loop.thinking.threads)
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()


def test_talking_completes_while_thinking_is_blocked(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = setup(store)
        session = store.create_session()
        result = loop.run_turn(session, "Compare these choices")
        assert provider.started.wait(2)
        assert result["message"]["content"] == "Talking reply"
        assert background_thinking.list_for_session(store, session)[0]["status"] == "running"
        wait_workers(loop, provider)
        row = background_thinking.list_for_session(store, session)[0]
        assert row["status"] == "completed" and row["result"] == "Useful findings"
        assert len(store.messages(session)) == 2


def test_new_input_supersedes_old_result_and_cancel_discards_new(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = setup(store)
        session = store.create_session()
        old = loop.run_turn(session, "First task")["turn_id"]
        new = loop.run_turn(session, "Changed task")["turn_id"]
        background_thinking.cancel(store, session, new)
        wait_workers(loop, provider)
        rows = {row["turn_id"]: row for row in background_thinking.list_for_session(store, session)}
        assert rows[old]["status"] == "superseded" and rows[new]["status"] == "canceled"
        assert all(row["result"] is None for row in rows.values())


def test_no_harness_prevents_background_use_and_restart_never_retries(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = setup(store)
        session = store.create_session(privacy={"harnessDisabled": True})
        loop.run_turn(session, "Private request")
        assert background_thinking.list_for_session(store, session) == []
        session = store.create_session()
        loop.run_turn(session, "Analysis")
        background_thinking.recover_interrupted(store)
        wait_workers(loop, provider)
        row = background_thinking.list_for_session(store, session)[0]
        assert row["status"] == "interrupted" and row["result"] is None
