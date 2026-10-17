"""Messages carry the turn that consumed or produced them, so callers can show its model and cost."""

from contextlib import closing

from pi.conversation.store import Store
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.turns.loop import Loop


class Recorder:
    name = "recorder"

    def complete(self, messages, *, model):
        return Completion(text="Recorded reply", provider=self.name, model=model)


def test_input_and_reply_name_their_turn(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop = Loop(store, Router(local_provider=Recorder(), local_model="test"))
        session = store.create_session()
        result = loop.run_turn(session, "Plan my week")
        messages = store.messages(session)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert {m["turn_id"] for m in messages} == {result["turn_id"]}
        assert store.get_message(messages[1]["id"])["turn_id"] == result["turn_id"]
        unbound = store.append_message(session, "user", "Saved without a turn")
        assert "turn_id" not in store.get_message(unbound["id"])
