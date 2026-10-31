"""Inbox cards never carry terminal control characters, whoever wrote their words."""

import json

import pytest

from pi.agents import change_requests, tool_drafts, workflow_drafts
from pi.conversation.store import Store
from pi.turns import native_tools
from tests.test_tool_drafts import DOCUMENT, build
from tests.test_tool_turns import FakeGate
from tests.test_tool_turns import build as companion

# An escape sequence that retitles the owner's terminal, and one that clears it.
RETITLE, CLEAR = "\x1b]0;pwned\x07", "\x1b[2J"


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def plain(text):
    return change_requests.CONTROL.search(text) is None


def test_a_drafted_tools_name_and_reply_reach_the_card_without_control_characters(store):
    nodes = [dict(node) for node in DOCUMENT["nodes"]]
    nodes[1]["label"] = f"Search{CLEAR} the web"
    document = {**DOCUMENT, "name": f"Topic{RETITLE} reader", "nodes": nodes}
    reply = {"reply": f"Reads{CLEAR} the first result.", "ready": True, "document": document}
    loop, _ = build(store, [json.dumps(reply)])
    filed = tool_drafts.file(store, "Search the web for a topic and fetch the first result")
    assert loop.drafts.tick() == 1
    request = change_requests.get(store, filed["id"])
    assert request["title"] == "Add the tool “Topic]0;pwned reader”"
    assert request["summary"] == "Reads[2J the first result."
    drafted = request["payload"]["document"]
    assert drafted["name"] == "Topic]0;pwned reader"
    assert all(plain(node["label"]) for node in drafted["nodes"])


def test_a_description_from_chat_files_a_plain_card(store):
    filed = tool_drafts.file(store, f"Fetch{CLEAR} the page{RETITLE}\r\nevery morning")
    assert filed["title"] == "Draft a tool: Fetch[2J the page]0;pwned every morning"
    assert filed["summary"] == "Fetch[2J the page]0;pwned\nevery morning"


def test_trying_again_from_a_new_description_keeps_the_card_plain(store):
    loop, _ = build(store, [json.dumps({"reply": "Which site?", "ready": False})])
    filed = tool_drafts.file(store, "Search the web")
    loop.drafts.tick()
    again = tool_drafts.retry(
        store, filed["id"], tool_drafts.Retry(description=f"Search{CLEAR} Bing{RETITLE}")
    )
    assert again["title"] == "Draft a tool: Search[2J Bing]0;pwned"
    assert plain(again["summary"])


def test_a_memory_card_is_plain_and_its_text_stays_exactly_as_asked(store):
    loop, _ = companion(store, [], FakeGate())
    text = f"The exam{RETITLE} is on Thursday."
    native_tools.call(
        loop, native_tools.REMEMBER, {"text": text}, session_id=None, turn_id=None, execution=None
    )
    [request] = change_requests.listing(store)
    assert request["title"] == "Remember: The exam]0;pwned is on Thursday."
    assert plain(request["summary"]) and request["payload"] == {"text": text}


def test_a_title_of_only_control_characters_gets_the_default():
    assert change_requests.title_text(RETITLE.replace("]0;pwned", "")) == "Conker's request"


def test_a_name_of_only_control_characters_is_missing():
    with pytest.raises(ValueError):
        workflow_drafts.text(CLEAR.replace("[2J", ""), 100)
    assert workflow_drafts.text(f"{CLEAR}", 100) == "[2J"
    assert workflow_drafts.text("\x07", 100, "Step") == "Step"
