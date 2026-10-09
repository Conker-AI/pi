"""Malformed action requests are neither executed nor published as answers."""

import json
from contextlib import closing

import pytest
from test_tool_turns import CALL, FakeGate, build

from pi.loop import ActedWithoutReply, TurnFailed
from pi.store import Store
from pi.tools import InvalidToolOutput, describe, parse, validate_answer


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


BAD_REQUESTS = [
    '{"tool":"None needed","args":{}}',
    '{"tool":"local_companion","args":{"scope":"conversation","query":"hello"}}',
    '{"tool":"t_echo","args":',
    "{'tool':'t_echo','args':{}}",
    '{"tool":"t_echo"}',
    '{"tool":"t_echo","args":null}',
    '{"tool":"t_echo","args":"hi"}',
    '{"tool":"t_echo","args":[]}',
    '{"tool":"t_echo","args":{},"approved":true}',
    '{"tool":42,"args":{}}',
    '{"tool":"unknown","tool":"t_echo","args":{}}',
    '{"tool":"t_echo","args":{"text":"one","text":"two"}}',
    '{"tool":"t_echo","args":{"number":NaN}}',
    '{"tool":"t_echo","args":{"number":1e999}}',
    '{"tool_calls":[{"function":{"name":"t_echo","arguments":"{}"}}]}',
    '{"type":"tool_use","name":"t_echo","input":{}}',
    '{"type":"tool_use","name":"t_echo","input":',
    f"[{CALL}]",
    f"```json\n{CALL}\n```",
    CALL + "\n" + CALL,
    CALL + "\nI ran this.",
    "Let me run this:\n" + CALL,
]


@pytest.mark.parametrize("output", BAD_REQUESTS)
def test_invalid_request_fails_without_dispatch_or_completed_answer(store, output):
    gate = FakeGate()
    loop, provider = build(store, [output, "must not automatically retry"], gate)
    sid = store.create_session()

    with pytest.raises(TurnFailed, match="not executed") as failed:
        loop.run_turn(sid, "Reply with one short sentence.")

    turn = store.get_turn(failed.value.turn_id)
    assert turn["status"] == "failed" and turn["acted"] == 0
    assert output not in turn["detail"]
    assert gate.invocations == [] and len(provider.sent) == 1
    assert [m["role"] for m in store.messages(sid)] == ["user"]
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tool_actions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "output",
    [
        "Yes, I can talk with you.",
        '{"answer":"Yes","count":1}',
        '{"data":{"tool":"A tool name inside ordinary data"}}',
        f"An example is {CALL}; this is only an explanation.",
        f"The request format is illustrated below:\n```json\n{CALL}\n```",
    ],
)
def test_plain_answers_and_quoted_examples_still_complete(store, output):
    gate = FakeGate()
    loop, _ = build(store, [output], gate)
    sid = store.create_session()
    result = loop.run_turn(sid, "Explain briefly.")
    assert result["message"]["content"] == output
    assert gate.invocations == []


@pytest.mark.parametrize("toolgate", [None, FakeGate(unavailable=True)])
def test_hallucinated_request_fails_when_no_tools_are_available(store, toolgate):
    loop, provider = build(store, [CALL], toolgate)
    with pytest.raises(TurnFailed, match="unavailable tool"):
        loop.run_turn(store.create_session(), "Hello")
    assert len(provider.sent) == 1


@pytest.mark.parametrize("bad_reply", [CALL, *BAD_REQUESTS[:7]])
def test_approval_result_survives_invalid_narration_and_reply_only_recovery(store, bad_reply):
    gate = FakeGate(needs_approval=True)
    loop, provider = build(store, [CALL, bad_reply, bad_reply, "The tool echoed hi."], gate)
    sid = store.create_session()
    parked = loop.run_turn(sid, "Echo hi")

    for _ in range(2):
        with pytest.raises(ActedWithoutReply):
            loop.resume_turn(parked["turn_id"])
        turn = store.get_turn(parked["turn_id"])
        assert turn["status"] == "acted_no_reply" and turn["acted"] == 1
        assert len(gate.invocations) == 2  # approval request + one approved effect
        assert len([m for m in store.messages(sid) if m["role"] == "tool"]) == 1
        assert not any(m["content"] == bad_reply for m in store.messages(sid)[2:])

    reply = loop.resume_turn(parked["turn_id"])
    assert reply["status"] == "complete" and reply["acted"] is True
    assert reply["message"]["content"] == "The tool echoed hi."
    assert len(gate.invocations) == 2 and len(provider.sent) == 4
    assert loop.run_turn(sid, "Thanks")["message"]["content"] == "done"
    assert len(gate.invocations) == 2


def test_invalid_second_request_preserves_first_result_without_retry(store):
    gate = FakeGate()
    loop, provider = build(store, [CALL, BAD_REQUESTS[0], "It echoed hi."], gate)
    sid = store.create_session()
    with pytest.raises(ActedWithoutReply) as failed:
        loop.run_turn(sid, "Echo hi")
    assert len(gate.invocations) == 1 and len(provider.sent) == 2
    assert loop.resume_turn(failed.value.turn_id)["message"]["content"] == "It echoed hi."
    assert len(gate.invocations) == 1


def test_action_ceiling_prompts_for_narration_and_never_executes_extra_request(store):
    gate = FakeGate()
    loop, provider = build(store, [CALL, CALL, "One echo finished."], gate)
    loop.max_tool_steps = 1
    with pytest.raises(ActedWithoutReply, match="answer was required") as failed:
        loop.run_turn(store.create_session(), "Echo hi")
    assert len(gate.invocations) == 1
    assert any("Do not repeat a tool call" in m.content for m in provider.sent[-1])
    assert not any(
        m.role == "system" and "Available tools:" in m.content for m in provider.sent[-1]
    )
    loop.resume_turn(failed.value.turn_id)
    assert len(gate.invocations) == 1


def test_strict_decoder_accepts_key_order_whitespace_and_exact_nested_args():
    args = {"text": "hello", "nested": {"array": [None, True, 1.25, "\u4e16\u754c"]}}
    text = json.dumps({"args": args, "tool": "t_echo"}, indent=2)
    call = parse(text, {"t_echo"})
    assert call.tool_id == "t_echo" and call.args == args
    with pytest.raises(InvalidToolOutput, match="answer was required"):
        validate_answer(text)


def test_protocol_work_is_bounded_and_diagnostics_never_echo_payload(monkeypatch):
    import pi.tools as tools

    monkeypatch.setattr(tools, "MAX_TOOL_REQUEST_CHARS", 50)
    output = json.dumps({"tool": "t_echo", "args": {"text": "SENSITIVE" * 10}})
    with pytest.raises(InvalidToolOutput) as failed:
        parse(output, {"t_echo"})
    assert "SENSITIVE" not in str(failed.value)


def test_prompt_distinguishes_ordinary_answers_from_tool_actions():
    prompt = describe(FakeGate().tools())
    assert "For an ordinary answer, write plain conversational text" in prompt
    assert "Do not use the tool JSON format" in prompt
    assert "Never request a tool named none" in prompt
    assert "Only when the user needs a listed tool" in prompt
    assert "Use the input schema for args" in prompt
