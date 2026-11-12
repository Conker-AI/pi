"""One agent runs another (`conker.run_agent`): its own chat, its reply back, and every guard."""

import json
import sqlite3
from contextlib import closing

import pytest
from test_access_layers import APPROVE, SEND
from test_access_layers import Loop as ReviewerLoop
from test_tool_turns import FakeGate, build

from pi.actions.toolgate import Tool
from pi.agents import agent_calls
from pi.agents import profiles as agents
from pi.conversation import forgetting
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models.providers import ProviderUnavailable
from pi.turns import access, native_tools
from pi.turns import control as turn_control

RUN = native_tools.RUN_AGENT
ECHO = json.dumps({"tool": "t_echo", "result": {"echoed": "a web page"}})


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "pi.db")
    yield value
    value.close()


def agent(store, name, tools=(RUN,)):
    return agents.create(
        store,
        agents.AgentInput(
            name=name,
            role="Helper",
            instructions=f"You are {name}.",
            modelId=None,
            toolIds=list(tools),
            memory=agents.MemorySelection(scope="none", memoryIds=[]),
        ),
    )


def chat(
    store, text="Plan my week.", *, agent_id=None, privacy=None, tool_messages=(), switch=None
):
    session = store.create_session(agent_id=agent_id, privacy=privacy)
    if switch:
        current = session_settings.load(store, session)
        session_settings.save(
            store,
            session,
            session_settings.Update(
                expected_revision=current["revision"],
                settings=session_settings.Settings(**{**current["settings"], "access": switch}),
            ),
        )
    turn = store.start_turn(session)
    store.append_message(session, "user", text, turn_id=turn, purpose="input")
    for content in tool_messages:
        store.append_message(session, "tool", content, turn_id=turn, purpose="tool_result")
    return session, turn


def call(loop, session, turn, target, instructions="Find three quiet cafes.", *, run=True):
    return native_tools.call(
        loop,
        RUN,
        {"agent": target, "instructions": instructions},
        session_id=session,
        turn_id=turn,
        execution=session_settings.execution(loop.store, session),
        run=run,
    )


def child_turn(store, child):
    [row] = store.turns(child)
    return row["id"]


def shown(store, result):
    """What the caller's model reads back from the stored record of a call."""
    text = json.dumps({"tool": RUN, "result": result.result})
    return json.loads(agent_calls.expand(store, "tool", text))["result"]


def rows(store):
    with store._connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM agent_calls ORDER BY created_at")]


def test_the_reply_comes_back_and_the_chat_is_linked(store):
    loop, provider = build(store, ["Cafe A, Cafe B, Cafe C."], FakeGate())
    helper = agent(store, "Scout")
    session, turn = chat(store)
    result = call(loop, session, turn, "scout")  # the exact name, any case
    assert result.ok and result.result["status"] == "done"
    # The caller's chat keeps a pointer only; its model reads the reply from the child chat.
    assert "reply" not in result.result and result.result["callId"].startswith("acall_")
    assert shown(store, result)["reply"] == "Cafe A, Cafe B, Cafe C."
    assert result.result["outside"] is False and result.result["agentName"] == "Scout"
    child = result.result["sessionId"]
    [row] = rows(store)
    assert (row["parent_session_id"], row["parent_turn_id"], row["child_session_id"]) == (
        session,
        turn,
        child,
    )
    assert (row["agent_id"], row["depth"], row["state"]) == (helper["id"], 1, "done")
    saved = store.get_session(child)
    # Its title names the agent only; the caller's instructions stay in the caller's chat.
    assert saved["parent_id"] == session and saved["title"] == "Agent call: Scout"
    execution = session_settings.execution(store, child, child_turn(store, child))
    assert execution["agentId"] == helper["id"]
    # The child starts empty: the caller's chat is not its history.
    sent = provider.sent[-1]
    assert not any("Plan my week." in str(m.content) for m in sent)
    assert any("Find three quiet cafes." in str(m.content) for m in sent)


def test_a_whole_turn_runs_another_agent_inline(store):
    agent(store, "Scout")
    asked = json.dumps({"tool": RUN, "args": {"agent": "Scout", "instructions": "Name one cafe."}})
    loop, provider = build(store, [asked, "Cafe A.", "Scout suggests Cafe A."], FakeGate())
    session = store.create_session()
    result = loop.run_turn(session, "Ask Scout for a cafe.")
    assert result["message"]["content"] == "Scout suggests Cafe A."
    [record] = [m for m in store.messages(session) if m["role"] == "tool"]
    assert "Cafe A." not in str(record["content"])
    assert any(m.role == "tool" and "Cafe A." in m.content for m in provider.sent[-1])
    [row] = rows(store)
    assert row["parent_turn_id"] == result["turn_id"] and row["state"] == "done"
    # His typed words and Conker's own result: nothing outside, since the result says so.
    assert not access.outside_content(store, session, "", native_tools.owns)


def test_an_unknown_or_archived_agent_is_refused(store):
    loop, _ = build(store, [], FakeGate())
    session, turn = chat(store)
    unknown = call(loop, session, turn, "Nobody")
    assert unknown.result["status"] == "refused" and unknown.result["outside"] is False
    helper = agent(store, "Scout")
    agents.archive(store, helper["id"], agents.ArchiveAgent(expected_revision=1, archived=True))
    archived = call(loop, session, turn, helper["id"])
    assert archived.result["status"] == "refused" and "archived" in archived.result["message"]
    assert rows(store) == [] and len(store.list_sessions()) == 1


def test_an_agent_cannot_run_itself(store):
    loop, _ = build(store, [], FakeGate())
    session, turn = chat(store)
    result = call(loop, session, turn, "companion")
    assert result.result["status"] == "refused" and rows(store) == []


def test_a_loop_back_up_the_chain_is_refused(store):
    a, _b = agent(store, "Alpha"), agent(store, "Beta")
    loop, _ = build(store, ["Beta here."], FakeGate())
    session, turn = chat(store, agent_id=a["id"])
    first = call(loop, session, turn, "Beta")
    assert first.result["status"] == "done"
    child = first.result["sessionId"]
    back = call(loop, child, child_turn(store, child), "Alpha")
    assert back.result["status"] == "refused" and "already working" in back.result["message"]
    assert len(rows(store)) == 1


def test_depth_two_is_the_last_and_is_not_offered_the_tool(store):
    agent(store, "Alpha"), agent(store, "Beta"), agent(store, "Gamma")
    loop, _ = build(store, ["Alpha here.", "Beta here."], FakeGate())
    session, turn = chat(store)
    one = call(loop, session, turn, "Alpha").result["sessionId"]
    two = call(loop, one, child_turn(store, one), "Beta").result["sessionId"]
    assert [r["depth"] for r in rows(store)] == [1, 2]
    deep_turn = child_turn(store, two)
    execution = session_settings.execution(store, two, deep_turn)
    assert RUN not in {t.id for t in native_tools.offered(loop, execution, two, deep_turn)}
    assert native_tools.refused(store, RUN, session_id=two, turn_id=deep_turn, execution=execution)
    three = call(loop, two, deep_turn, "Gamma")
    assert three.result["status"] == "refused" and len(rows(store)) == 2
    # The guard holds even if the offer check were skipped.
    with pytest.raises(agent_calls.Refused):
        agent_calls._open(
            store,
            "Gamma",
            session_id=two,
            turn_id=None,
            execution=execution,
            outside=False,
            request_id="agentcall_x",
        )


def test_a_fourth_call_in_one_turn_is_refused(store):
    agent(store, "Scout")
    loop, _ = build(store, ["One.", "Two.", "Three."], FakeGate())
    session, turn = chat(store)
    for _ in range(3):
        assert call(loop, session, turn, "Scout").result["status"] == "done"
    execution = session_settings.execution(store, session)
    assert RUN not in {t.id for t in native_tools.offered(loop, execution, session, turn)}
    fourth = call(loop, session, turn, "Scout")
    assert fourth.result["status"] == "refused" and "3 agents" in fourth.result["message"]
    assert len(rows(store)) == 3


def test_thirty_calls_a_day_in_all(store):
    agent(store, "Scout")
    session, turn = chat(store)
    with store._connect() as db:
        for number in range(agent_calls.PER_DAY):
            other = store.create_session()
            db.execute(
                "INSERT INTO agent_calls(id,parent_session_id,parent_turn_id,child_session_id,"
                "agent_id,depth,parent_outside,state,created_at) VALUES (?,?,?,?,?,1,0,'done',?)",
                (f"acall_{number}", session, f"trn_{number}", other, "companion", 1e12),
            )
        db.commit()
    assert "24 hours" in agent_calls.refused(store, session, turn, now=1e12 + 60)
    assert agent_calls.refused(store, session, turn, now=1e12 + agent_calls.DAY + 1) is None


def test_the_child_turn_has_no_owner_request(store):
    agent(store, "Mailer", tools=())
    loop, _ = build(store, ["Ready."], FakeGate())
    session, turn = chat(store)
    result = call(loop, session, turn, "Mailer", "Send Dani the notes")
    child = result.result["sessionId"]
    turn_in_child = child_turn(store, child)
    assert access.owner_text(store, turn_in_child) == ""
    # The same words he typed himself would be approved by the reviewer; there they only ask.
    reviewer = ReviewerLoop(store, [APPROVE])
    decision = access.decide(
        reviewer,
        SEND,
        {"to": "Dani"},
        execution={"settings": {"access": "default"}, "configuration": {"toolAccess": {}}},
        session_id=child,
        turn_id=turn_in_child,
        native=native_tools.owns,
        delegate=lambda: True,
    )
    assert decision[0] == "ask" and reviewer.reviews == []
    _his, his_turn = chat(store, "Send Dani the notes")
    assert access.owner_text(store, his_turn) == "Send Dani the notes"


def test_a_caller_that_read_outside_content_makes_the_child_outside(store):
    agent(store, "Scout")
    loop, _ = build(store, ["Summary.", "Clean."], FakeGate())
    tainted, turn = chat(store, tool_messages=[ECHO])
    result = call(loop, tainted, turn, "Scout")
    assert result.result["outside"] is True
    child = result.result["sessionId"]
    assert access.outside_content(store, child, "", native_tools.owns)
    assert rows(store)[0]["parent_outside"] == 1
    clean, clean_turn = chat(store)
    other = call(loop, clean, clean_turn, "Scout")
    assert other.result["outside"] is False
    assert not access.outside_content(store, other.result["sessionId"], "", native_tools.owns)


def test_the_child_chat_is_memory_off_keeps_privacy_and_only_ask_of_access(store):
    agent(store, "Scout")
    loop, _ = build(store, ["One.", "Two.", "Three.", "Four."], FakeGate())
    cases = [
        (None, "full", "default"),
        ({"memoryDisabled": True, "harnessDisabled": True}, "ai", "default"),
        (None, "ask", "ask"),
        (None, None, "default"),
    ]
    for privacy, switch, expected in cases:
        session, turn = chat(store, privacy=privacy, switch=switch)
        result = call(loop, session, turn, "Scout")
        child = result.result["sessionId"]
        settings = session_settings.load(store, child)["settings"]
        assert settings["privacy"]["memoryDisabled"] is True
        assert settings["privacy"]["harnessDisabled"] is bool(privacy)
        # His loosening was for his chat and its agent, never for another agent's toolkit.
        assert settings["access"] == expected
        snapshot = session_settings.execution(store, child, child_turn(store, child))
        assert not session_settings.memory_allowed(snapshot)
        # Nothing in it reaches memory, and it may not remember or schedule.
        assert native_tools.private(store, child, snapshot)


def test_every_result_carries_outside_and_the_caller_trusts_only_false(store):
    loop, _ = build(store, [], FakeGate())
    session, turn = chat(store)
    invalid = call(loop, session, turn, "Scout", "")
    assert invalid.result == {
        "status": "invalid",
        "message": "Give instructions as text.",
        "outside": False,
    }
    for result, outside in (({"outside": False}, False), ({"outside": True}, True), ({}, True)):
        observation = json.dumps({"tool": RUN, "result": {"status": "done", **result}})
        marked, _ = chat(store, tool_messages=[observation])
        assert access.outside_content(store, marked, "", native_tools.owns) is outside


def test_a_failed_child_is_an_answer_not_an_error(store):
    from pi.models.providers import ProviderUnavailable

    agent(store, "Scout")
    loop, _ = build(store, [ProviderUnavailable("down")], FakeGate())
    session, turn = chat(store)
    result = call(loop, session, turn, "Scout")
    assert not result.ok and result.result["status"] == "failed"
    assert result.result["outside"] is False and rows(store)[0]["state"] == "failed"


def test_when_his_settings_ask_first_it_says_to_ask_him(store):
    agent(store, "Scout")
    loop, _ = build(store, [], FakeGate())
    session, turn = chat(store)
    result = call(loop, session, turn, "Scout", run=False)
    assert result.result["status"] == "refused" and "conker.ask" in result.result["message"]
    assert result.result["outside"] is False and rows(store) == []


def test_forgetting_the_callers_chat_removes_the_calls_and_the_child(store, tmp_path):
    agent(store, "Scout")
    loop, _ = build(store, ["Done."], FakeGate())
    session, turn = chat(store)
    child = call(loop, session, turn, "Scout").result["sessionId"]
    store.close()
    path = tmp_path / "pi.db"
    plan = forgetting.preview(path, session)
    assert child in plan["session_ids"]
    forgetting.forget(path, session, plan["confirmation"])
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_calls").fetchone()[0] == 0
        assert db.execute("SELECT status FROM sessions WHERE id=?", (child,)).fetchone()[0] == (
            "forgotten"
        )


def test_a_full_access_caller_does_not_loosen_the_childs_tools(store):
    agent(store, "Mailer", tools=("t_echo",))
    loop, _ = build(store, ["Ready."], FakeGate())
    session, turn = chat(store, switch="full")
    child = call(loop, session, turn, "Mailer").result["sessionId"]
    confirm = Tool("t_echo", "echo", "echoes things", [], "owner_confirmation")
    in_child = child_turn(store, child)
    decision = access.decide(
        loop,
        confirm,
        {"text": "hi"},
        execution=session_settings.execution(store, child, in_child),
        session_id=child,
        turn_id=in_child,
        native=native_tools.owns,
        delegate=lambda: True,
    )
    assert decision[0] == "ask"


def test_trashing_or_forgetting_the_child_takes_the_reply_from_the_caller(store):
    agent(store, "Scout")
    loop, _ = build(store, ["The secret plan."], FakeGate())
    session, turn = chat(store)
    result = call(loop, session, turn, "Scout")
    child = result.result["sessionId"]
    assert shown(store, result)["reply"] == "The secret plan."
    with store._connect() as db:
        db.execute("INSERT INTO session_trash VALUES (?,?)", (child, 1.0))
        db.commit()
    gone = shown(store, result)
    assert gone["status"] == "gone" and "reply" not in gone
    with store._connect() as db:
        db.execute("DELETE FROM session_trash WHERE session_id=?", (child,))
        db.commit()
    assert shown(store, result)["reply"] == "The secret plan."
    # The reply in a continuation of the child chat: trashing that chat hides it too.
    fork = store.create_session(parent_id=child)
    in_fork = store.append_message(fork, "assistant", "The secret plan.")["id"]
    with store._connect() as db:
        db.execute("UPDATE agent_calls SET reply_message_id=?", (in_fork,))
        db.commit()
    assert shown(store, result)["reply"] == "The secret plan."
    with store._connect() as db:
        db.execute("INSERT INTO session_trash VALUES (?,?)", (fork, 1.0))
        db.commit()
    assert shown(store, result)["status"] == "gone"
    with store._connect() as db:
        agent_calls.redact(db, [child])
        db.commit()
    assert "The secret plan." not in json.dumps(shown(store, result))


def test_a_stop_on_the_caller_reaches_the_child(store, monkeypatch):
    agent(store, "Scout")
    loop, _ = build(store, ["Never sent."], FakeGate())
    session, turn = chat(store)
    turn_control.cancel(store, turn)
    before = call(loop, session, turn, "Scout")
    assert before.result["status"] == "refused" and "stopped" in before.result["message"]
    # Even past that check (a Stop that lands as the child starts), its own guard stops it.
    monkeypatch.setattr(agent_calls, "_stopped", lambda *args: False)
    late = call(loop, session, turn, "Scout")
    assert late.result["status"] == "failed"
    child = late.result["sessionId"]
    with store._connect() as db, pytest.raises(ProviderUnavailable, match="started this agent"):
        turn_control.guard_db(db, child_turn(store, child))


def test_stopping_the_caller_stops_its_running_calls(store, monkeypatch):
    session, turn = chat(store)
    other = store.create_session()
    with store._connect() as db:
        db.execute(
            "INSERT INTO agent_calls(id,parent_session_id,parent_turn_id,child_session_id,"
            "agent_id,depth,parent_outside,state,created_at,request_id) "
            "VALUES ('acall_r',?,?,?,'x',1,0,'running',1,'agentcall_r')",
            (session, turn, other),
        )
        db.commit()
    stopped = []
    real = turn_control.cancel_submission
    monkeypatch.setattr(
        turn_control,
        "cancel_submission",
        lambda store, request_id: stopped.append(request_id) or real(store, request_id),
    )
    turn_control.cancel(store, turn)  # its submission does not exist: that is no error
    assert stopped == ["agentcall_r"]


def test_a_child_that_acted_marks_the_caller_acted(store):
    agent(store, "Echoer", tools=("t_echo",))
    echo = json.dumps({"tool": "t_echo", "args": {"text": "hi"}})
    loop, _ = build(store, [echo, "Echoed."], FakeGate())
    session, turn = chat(store)
    assert not store.get_turn(turn)["acted"]
    assert call(loop, session, turn, "Echoer").result["status"] == "done"
    assert store.get_turn(turn)["acted"]


def test_a_continuation_of_the_child_chat_is_still_that_calls_chat(store):
    agent(store, "Scout")
    loop, _ = build(store, ["Done."], FakeGate())
    tainted, turn = chat(store, tool_messages=[ECHO])
    child = call(loop, tainted, turn, "Scout").result["sessionId"]
    store.close_session(child, "forked", summary="so far")
    fork = store.create_session(parent_id=child, summary="so far")
    fork_turn = store.start_turn(fork)
    store.append_message(fork, "user", "Send it all", turn_id=fork_turn, purpose="input")
    assert access.owner_text(store, fork_turn) == ""
    assert access.outside_content(store, fork, "", native_tools.owns)
    with store._connect() as db:
        assert agent_calls.depth(db, fork) == 1
        assert agent_calls.caller_turn(db, fork_turn) == turn


def test_a_team_role_or_an_ai_job_never_runs_an_agent(store):
    session, turn = chat(store)
    role = {**session_settings.execution(store, session), "kind": "team-role"}
    assert "team role" in native_tools.refused(
        store, RUN, session_id=session, turn_id=turn, execution=role
    )
    assert RUN in native_tools.JOB_REFUSED
