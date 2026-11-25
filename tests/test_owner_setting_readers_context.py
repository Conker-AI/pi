"""Owner settings that shape a turn's context and Conker's own memories; each test fails if its
reader ignores the value."""

import json
from contextlib import closing

import pytest
from test_conker_actions import call
from test_deep_research import PLAN
from test_research_fetch import FETCH, FetchGate
from test_tool_turns import FakeGate, build
from test_web_research import SEARCH

from pi.actions.toolgate import ToolResult
from pi.context import controls as context_controls
from pi.conversation.store import Store
from pi.core import owner_settings as owner_settings
from pi.core.context_bodies import Update as ContextUpdate
from pi.turns import native_tools


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))


def seen(provider) -> list[str]:
    """What the model was given in its last call, without the system prefix."""
    return [m.content for m in provider.sent[-1] if m.role != "system"]


def chatted(store, turns=4):
    """A chat of `turns` exchanges q1/a1 ... and a loop whose next answer is "now"."""
    replies = [f"a{n}" for n in range(1, turns + 1)] + ["now"]
    loop, provider = build(store, replies, FakeGate())
    session = store.create_session()
    for n in range(1, turns + 1):
        loop.run_turn(session, f"q{n}")
    return loop, provider, session


# context.history, context.recent_messages


def test_everything_is_sent_unless_the_owner_chooses_less(store):
    loop, provider, session = chatted(store)
    loop.run_turn(session, "latest")
    assert seen(provider) == ["q1", "a1", "q2", "a2", "q3", "a3", "q4", "a4", "latest"]


def test_recent_messages_sends_only_the_last_ones(store):
    loop, provider, session = chatted(store, turns=4)
    choose(store, {"context.history": "recent", "context.recent_messages": 5})
    loop.run_turn(session, "latest")
    assert seen(provider) == ["a2", "q3", "a3", "q4", "a4", "latest"]
    # The transcript keeps everything.
    assert [m["content"] for m in store.messages(session)][:2] == ["q1", "a1"]


def test_summary_only_sends_none_of_the_earlier_messages(store):
    loop, provider, session = chatted(store, turns=2)
    choose(store, {"context.history": "summary"})
    loop.run_turn(session, "latest")
    assert seen(provider) == ["latest"]


def test_a_chat_with_its_own_context_rules_keeps_them(store):
    loop, provider, session = chatted(store, turns=2)
    choose(store, {"context.history": "summary"})
    context_controls.save(
        store,
        session,
        ContextUpdate(
            expected_revision=0,
            policy={
                "sessionInstructions": "",
                "messagePolicies": {},
                "budget": {
                    "contextWindowTokens": 100000,
                    "outputReserveTokens": 1,
                    "otherInputTokens": 0,
                },
            },
        ),
    )
    loop.run_turn(session, "latest")
    assert seen(provider) == ["q1", "a1", "q2", "a2", "latest"]


@pytest.mark.parametrize("count", [3, 500, "ten", True])
def test_recent_messages_out_of_range_counts_as_thirty(store, count):
    loop, provider, session = chatted(store, turns=2)
    choose(store, {"context.history": "recent", "context.recent_messages": count})
    loop.run_turn(session, "latest")
    assert seen(provider) == ["q1", "a1", "q2", "a2", "latest"]


# context.web_keep


def research_then_ask(store, keep=None):
    if keep:
        choose(store, {"context.web_keep": keep})
    loop, provider = build(store, [PLAN, SEARCH, FETCH, "Sourced answer", "Later"], FetchGate())
    session = store.create_session()
    loop.run_turn(session, "Investigate", research_mode="deep")
    assert any("Bounded source excerpt" in text for text in seen(provider))
    loop.run_turn(session, "And then?")
    return " ".join(seen(provider))


def test_a_fetched_page_stays_for_one_answer_by_default(store):
    later = research_then_ask(store)
    assert "Bounded source excerpt" not in later
    assert "left out" in later


def test_a_fetched_page_can_stay_for_the_whole_chat(store):
    assert "Bounded source excerpt" in research_then_ask(store, "chat")


# tools.result_limit


class BigGate(FakeGate):
    def invoke(self, tool_id, args, approval_request_id=None, **kwargs):
        self.invocations.append((tool_id, args, approval_request_id))
        return ToolResult(ok=True, result={"text": "é" * 40_000}, tool_id=tool_id)


@pytest.mark.parametrize(("limit", "most"), [(None, 64), (1, 1)])
def test_a_tool_result_holds_at_most_the_owners_limit(store, limit, most):
    if limit:
        choose(store, {"tools.result_limit": limit})
    call_echo = json.dumps({"tool": "t_echo", "args": {"text": "big"}})
    loop, provider = build(store, [call_echo, "done"], BigGate())
    session = store.create_session()
    loop.run_turn(session, "use the tool")
    [result] = [m.content for m in provider.sent[-1] if m.role == "tool"]
    shown, _, note = result.partition("\n[Cut here")
    assert len(shown.encode()) <= most * 1024 and f"at most {most} KB" in note
    # The transcript keeps the whole result.
    [stored] = [m["content"] for m in store.messages(session) if m["role"] == "tool"]
    assert len(stored.encode()) > 80_000


# memory.never


def remember(store, text):
    loop, _ = build(store, [], FakeGate())
    loop.remember = lambda identity, text, origin="owner": {"memoryId": "m1", "status": "ok"}
    return call(loop, native_tools.REMEMBER, {"text": text}, store.create_session(), run=True)


def memory_requests(store):
    with store._connect() as db:
        return db.execute("SELECT COUNT(*) FROM change_requests WHERE kind='memory'").fetchone()[0]


def test_the_default_topics_are_never_remembered(store):
    refused = remember(store, "The owner's HEALTH insurance renews in May.")
    assert refused.result["status"] == "refused" and "Health" in refused.result["message"]
    assert memory_requests(store) == 0
    assert remember(store, "The owner drinks tea.").result["status"] == "done"


def test_the_owners_own_topics_replace_the_default(store):
    choose(store, {"memory.never": [" Cats ", 4]})
    assert remember(store, "The owner's health plan renews in May.").result["status"] == "done"
    assert remember(store, "The owner has two cats.").result["status"] == "refused"
    choose(store, {"memory.never": []})
    assert remember(store, "The owner has two cats.").result["status"] == "done"
