"""`conker.run_agent`: what counts as a call's chat, where the child's turn is read, and which
agents may be run (hunt 2, tools-F1, F3, F5)."""

import pytest
from test_agent_calls import ECHO, agent, call, chat, rows
from test_tool_turns import FakeGate, build

from pi.agents import agent_calls
from pi.conversation import forks
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.turns import access, native_tools


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "pi.db")
    yield value
    value.close()


def _branch(store, session, message_id):
    settings = session_settings.load(store, session)
    return forks.create(
        store,
        session,
        message_id,
        forks.Fork(
            request_id="branch_" + "a" * 16,
            expected_settings_revision=settings["revision"],
            expected_context_revision=0,
        ),
    )["session_id"]


def test_a_message_branch_of_the_child_chat_is_still_that_calls_chat(store):
    agent(store, "Scout")
    loop, _ = build(store, ["Done."], FakeGate())
    tainted, turn = chat(store, tool_messages=[ECHO])
    child = call(loop, tainted, turn, "Scout").result["sessionId"]
    final = [m for m in store.messages(child) if m["role"] == "assistant"][-1]["id"]
    branch = _branch(store, child, final)
    # The source stays open: a message branch is not an automatic fork.
    assert store.get_session(child)["status"] == "open"
    branch_turn = store.start_turn(branch)
    store.append_message(branch, "user", "Go ahead", turn_id=branch_turn, purpose="input")
    assert access.owner_text(store, branch_turn) == ""
    assert access.outside_content(store, branch, "", native_tools.owns)
    with store._connect() as db:
        assert agent_calls.depth(db, branch) == 1
        assert agent_calls.caller_turn(db, branch_turn) == turn


def test_a_branch_of_an_ordinary_chat_is_no_calls_chat(store):
    loop, _ = build(store, ["Hello."], FakeGate())
    session = store.create_session()
    reply = loop.run_turn(session, "Hi.")["message"]["id"]
    branch = _branch(store, session, reply)
    with store._connect() as db:
        assert agent_calls.depth(db, branch) == 0


def test_a_forked_child_is_read_in_the_fork_it_ran_in(store):
    agent(store, "Scout")
    loop, _ = build(store, ["Done."], FakeGate())
    loop.fork_threshold_chars = 50
    clean, turn = chat(store)
    result = call(loop, clean, turn, "Scout", "Find three quiet cafes. " * 10)
    child = result.result["sessionId"]
    assert store.get_session(child)["status"] == "forked"
    # Nothing outside was read anywhere: the fork is read, not guessed as outside.
    assert result.result["status"] == "done" and result.result["outside"] is False


def test_a_forked_child_that_read_outside_content_says_so(store):
    agent(store, "Scout")
    loop, _ = build(store, ["Done."], FakeGate())
    loop.fork_threshold_chars = 50
    clean, turn = chat(store)
    seen = []

    def outside(session_id, turn_id, execution):
        seen.append(session_id)
        return session_id != clean

    result = agent_calls.run(
        loop,
        {"agent": "Scout", "instructions": "Find three quiet cafes. " * 10},
        session_id=clean,
        turn_id=turn,
        execution=session_settings.execution(store, clean),
        run=True,
        outside=outside,
    )
    child = result[1]["sessionId"]
    # The check ran on the chat the child's turn lives in, and its answer was kept.
    assert seen[-1] != child and store.get_session(seen[-1])["parent_id"] == child
    assert result[1]["outside"] is True


def test_the_companion_is_never_run_by_another_agent(store):
    narrow = agent(store, "Narrow")
    loop, _ = build(store, ["Done."], FakeGate())
    session, turn = chat(store, agent_id=narrow["id"])
    result = call(loop, session, turn, "companion")
    assert result.ok is False and result.result["status"] == "refused"
    assert "owner" in result.result["message"] and rows(store) == []
    assert len(store.list_sessions()) == 1
