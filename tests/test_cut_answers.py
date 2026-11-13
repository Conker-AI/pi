"""A reply the model stopped at its length limit says so, so a cut answer never reads as whole
(hunt pi-8 F5: nothing read `finish_reason`, and the cut text was stored as complete)."""

import json
from contextlib import closing

import pytest
from test_tool_turns import FakeGate, Scripted

from pi.conversation.store import Store
from pi.models.providers import CUT_NOTE, Completion, shown_text
from pi.models.routing import Router
from pi.turns.loop import Loop


class Finishing(Scripted):
    """Answers once, with the stop reason an adapter reports."""

    def __init__(self, raw):
        super().__init__([])
        self.raw = raw

    def complete(self, messages, *, model):
        return Completion(text="The first half of", model=model, provider=self.name, raw=self.raw)


@pytest.fixture()
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        yield store


def reply(store, raw):
    router = Router(local_provider=Finishing(raw), local_model="test-model")
    session = store.create_session()
    Loop(store, router, toolgate=FakeGate()).run_turn(session, "Tell me everything.")
    with store._connect() as db:
        row = db.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant'", (session,)
        ).fetchone()
    return json.loads(row[0])


@pytest.mark.parametrize(
    "raw", [{"finish_reason": "max_tokens"}, {"finish_reason": "length"}, {"done_reason": "length"}]
)
def test_a_reply_cut_at_the_length_limit_says_so(store, raw):
    assert CUT_NOTE in reply(store, raw)


@pytest.mark.parametrize("raw", [{}, {"finish_reason": "stop"}, {"done_reason": "stop"}])
def test_a_whole_reply_is_stored_as_written(store, raw):
    assert CUT_NOTE not in reply(store, raw)


def test_an_empty_cut_reply_gets_no_note():
    assert (
        shown_text(Completion(text="", model="m", provider="p", raw={"finish_reason": "length"}))
        == ""
    )
