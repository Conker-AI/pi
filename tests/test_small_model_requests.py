"""What small local models get wrong in a tool request: one narrow repair, a clear template,
and a retry note that names the mistake. Nothing from a rejected reply ever runs."""

import json
from contextlib import closing

import pytest
from test_tool_turns import CALL, FakeGate, build

from pi.actions import tools as tool_protocol
from pi.actions.toolgate import Tool
from pi.actions.tools import InvalidToolOutput, describe, parse, template, validate_answer
from pi.conversation.store import Store
from pi.turns.failures import TurnFailed

# A model writes `\$` in a shell command: an invalid JSON escape, so strict JSON refuses it.
ESCAPED = r'{"tool": "sandbox.run", "args": {"command": "getent hosts pypi.org ; echo exit=\$?"}}'
ECHO_HOME = r'{"tool": "t_echo", "args": {"text": "\$HOME"}}'
RUN = Tool(
    "sandbox.run",
    "Run a command",
    "Runs one command in the sandbox",
    [
        {"name": "command", "type": "string", "required": True, "description": "The command"},
        {"name": "cwd", "type": "string", "required": False},
        {"name": "timeout_seconds", "type": "number", "required": False},
    ],
)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_invalid_escapes_alone_are_dropped_and_the_request_decodes():
    call = parse(ESCAPED, {"sandbox.run"})
    assert call.tool_id == "sandbox.run"
    assert call.args == {"command": "getent hosts pypi.org ; echo exit=$?"}


def test_valid_escapes_are_never_touched():
    text = (
        r'{"tool": "t", "args": {"a": "line\nnext \"quoted\" back\\slash \/ '
        "\\"  # a \u escape
        r'u00e9 \t", "b": "\\$"}}'
    )
    assert parse(text, {"t"}).args == json.loads(text)["args"]
    mixed = r'{"tool": "t", "args": {"a": "say \"hi\"\n\$HOME \\ \?"}}'
    assert parse(mixed, {"t"}).args == {"a": 'say "hi"\n$HOME \\ ?'}


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (r'{"tool": "elsewhere", "args": {"command": "echo \$?"}}', "unavailable tool"),
        (r'{"tool": "sandbox.run", "args": {"command": "\$"}, "run": true}', "only tool"),
        (r'{"tool": "sandbox.run", "args": {"command": "\$", "command": "x"}}', "ambiguous"),
        ("```json\n" + ESCAPED + "\n```", "ambiguous"),
    ],
)
def test_a_repaired_request_still_goes_through_every_check(text, match):
    with pytest.raises(InvalidToolOutput, match=match):
        parse(text, {"sandbox.run"})


@pytest.mark.parametrize(
    "text",
    [
        # Another mistake besides the escape: nothing else is repaired.
        r'{"tool": "sandbox.run", "args": {"command": "echo \$?",}}',
        r'{"tool": "sandbox.run", "args": {"command": "echo \$?"}',
        # A backslash outside a string, and a broken \u escape, are not escapes to drop.
        r'{"tool": "sandbox.run", \"args": {"command": "x"}}',
        r'{"tool": "sandbox.run", "args": {"command": "\u12"}}',
    ],
)
def test_nothing_else_is_repaired(text):
    with pytest.raises(InvalidToolOutput) as failed:
        parse(text, {"sandbox.run"})
    assert failed.value.reason == tool_protocol.MALFORMED


def test_the_repaired_request_runs_without_a_retry(store):
    gate = FakeGate()
    loop, provider = build(
        store, [r'{"tool": "t_echo", "args": {"text": "exit=\$?"}}', "Done."], gate
    )
    result = loop.run_turn(store.create_session(), "Echo the exit code.")
    assert result["message"]["content"] == "Done."
    assert gate.invocations == [("t_echo", {"text": "exit=$?"}, None)]
    assert len(provider.sent) == 2


def test_a_repaired_request_is_still_not_an_answer():
    with pytest.raises(InvalidToolOutput, match="answer was required"):
        validate_answer(ESCAPED)


def test_tool_list_shows_each_tool_inputs_as_a_json_template():
    draft = Tool(
        "conker.draft_tool",
        "Draft a new tool",
        "Drafts a tool",
        [{"name": "description", "type": "string", "required": True, "description": "What"}],
    )
    every = Tool(
        "kinds",
        "Kinds",
        "",
        [
            {"name": "flag", "type": "boolean", "required": True},
            {"name": "items", "type": "array"},
            {"name": "options", "type": "object", "required": True},
            {"name": "plain"},
        ],
    )
    prompt = describe([draft, RUN, every, Tool("bare", "Bare", "", [])])
    assert '| args: {"description": "<text>"}' in prompt
    assert (
        '| args: {"command": "<text>", "cwd": "<text, optional>", '
        '"timeout_seconds": "<number, optional>"}'
    ) in prompt
    assert template(every.inputs) == (
        '{"flag": "<true or false>", "items": "<list, optional>", "options": "<object>", '
        '"plain": "<text, optional>"}'
    )
    assert "- bare (Bare): no description | args: {}" in prompt
    assert "description: string" not in prompt
    # The per-input reference lines stay.
    references = [line for line in prompt.splitlines() if "Input reference: " in line]
    assert [json.loads(line.split("Input reference: ", 1)[1])["name"] for line in references] == [
        "description",
        "command",
    ]


def retried(store, output):
    """The retry note a rejected reply gets, after checking that nothing ran or was kept."""
    gate = FakeGate()
    loop, provider = build(store, [output, output, "must not retry twice"], gate)
    with pytest.raises(TurnFailed, match="not executed") as failed:
        loop.run_turn(store.create_session(), "Do it.")
    assert gate.invocations == [] and len(provider.sent) == 2
    assert output not in store.get_turn(failed.value.turn_id)["detail"]
    note = provider.sent[1][-1].content
    assert note.startswith("Your last reply was not used") and output not in note
    return note


@pytest.mark.parametrize(
    ("output", "named"),
    [
        ("Let me run this:\n" + CALL, tool_protocol.PROSE),
        (CALL + "\nI ran this.", "more after the JSON request"),
        ("{'tool':'t_echo','args':{}}", "Expecting property name enclosed in double quotes"),
        ('{"tool":"t_echo","args":', "Expecting value"),
        (r'{"tool": "t_echo", "args": {"text": "\$",}}', tool_protocol.ESCAPES),
        ('{"text": "hi"}', 'without {"tool"'),
    ],
)
def test_the_retry_note_names_the_mistake(store, output, named):
    assert named in retried(store, output)


@pytest.mark.parametrize("key", ["text: ", "text:", " text", "text: string", '"text"'])
def test_an_input_name_with_something_added_is_retried_and_named_from_the_tool_list(store, key):
    output = json.dumps({"tool": "t_echo", "args": {key: "hi"}})
    note = retried(store, output)
    assert tool_protocol.KEY in note
    assert 'with nothing added: "text".' in note


def test_the_retry_runs_the_corrected_request(store):
    gate = FakeGate()
    loop, provider = build(
        store, ['{"tool": "t_echo", "args": {"text: ": "hi"}}', CALL, "Hi."], gate
    )
    assert loop.run_turn(store.create_session(), "Echo hi.")["message"]["content"] == "Hi."
    assert gate.invocations == [("t_echo", {"text": "hi"}, None)] and len(provider.sent) == 3


def test_an_unknown_key_is_left_to_toolgate(store):
    gate = FakeGate()
    loop, provider = build(store, ['{"tool": "t_echo", "args": {"words": "hi"}}', "Sent."], gate)
    loop.run_turn(store.create_session(), "Echo hi.")
    assert gate.invocations == [("t_echo", {"words": "hi"}, None)] and len(provider.sent) == 2


@pytest.mark.parametrize(
    "output",
    [
        f"I'll use t_echo to say hi:\n```json\n{CALL}\n```",
        f"Sure! Let me run the echo tool for you.\n\n```json\n{CALL}\n```",
        f"I am going to call t_echo now.\n```\n{CALL}\n```\nIt will echo hi.",
        f"```json\n{CALL}\n```\nI will now run this.",
        # A typographic apostrophe, and an invalid escape inside the quoted request.
        "I" + chr(0x2019) + "m using the echo tool:\n```json\n" + ECHO_HOME + "\n```",
    ],
)
def test_a_narrated_request_in_a_code_block_is_retried_and_never_run(store, output):
    assert tool_protocol.QUOTED in retried(store, output)


@pytest.mark.parametrize(
    "output",
    [
        f"The request format is illustrated below:\n```json\n{CALL}\n```",
        f"I'll explain the format with an example:\n```json\n{CALL}\n```",
        f"I'll show two requests:\n```json\n{CALL}\n```\nand\n```json\n{CALL}\n```",
        'I\'ll use another tool:\n```json\n{"tool": "elsewhere", "args": {}}\n```',
        'I\'ll run it:\n```json\n{"text": "hi"}\n```',
    ],
)
def test_an_explanation_with_a_quoted_example_is_still_an_answer(store, output):
    gate = FakeGate()
    loop, provider = build(store, [output], gate)
    result = loop.run_turn(store.create_session(), "Explain briefly.")
    assert result["message"]["content"] == output
    assert gate.invocations == [] and len(provider.sent) == 1


def test_a_second_invalid_reply_fails_the_turn(store):
    gate = FakeGate()
    first = f"I'll use t_echo:\n```json\n{CALL}\n```"
    loop, provider = build(store, [first, '{"tool": "t_echo", "args": {"text: ": "hi"}}'], gate)
    with pytest.raises(TurnFailed, match="something added"):
        loop.run_turn(store.create_session(), "Echo hi.")
    assert gate.invocations == [] and len(provider.sent) == 2
