"""Shared builders for the tool-draft tests: a scripted drafter and gate, a configured store, a built loop."""

import json

import pytest

from pi.actions.toolgate import Tool, ToolGateUnavailable
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.turns.loop import Loop
from tests.test_model_roles import config
from tests.test_tool_turns import Scripted

DIGEST = "c" * 64


TOOLS = [
    Tool("web.search", "Web search", "Search the web for a topic", [{"name": "query"}], "auto"),
    Tool("web.fetch", "Fetch page", "Fetch one web page", [{"name": "url"}], "auto"),
    Tool("files.list", "List files", "List the files in a folder", [{"name": "folder"}]),
    Tool(f"workflow:daily-brief:3:{DIGEST}", "Daily brief", "A short brief of my day", []),
    Tool("conker.remember", "Remember", "Pi's own tool", [{"name": "text"}]),
]


DOCUMENT = {
    "name": "Topic reader",
    "description": "Searches the web for a topic and reads the first result.",
    "inputs": [{"name": "topic", "type": "string", "required": True}],
    "outputs": [],
    "nodes": [
        {"id": "start", "type": "input", "label": "Start", "config": {}},
        {
            "id": "search",
            "type": "tool_call",
            "label": "Search the web for the topic",
            "config": {"tool": "web.search", "args": {"query": "$input.topic"}},
        },
        {
            "id": "fetch",
            "type": "tool_call",
            "label": "Fetch the first result",
            "config": {"tool": "web.fetch", "args": {"url": "$steps.search.first"}},
        },
        {"id": "done", "type": "return", "label": "Done", "config": {"value": "$last"}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "search"},
        {"id": "e2", "source": "search", "target": "fetch"},
        {"id": "e3", "source": "fetch", "target": "done"},
    ],
    "effect": "read",
}


READY = json.dumps(
    {"reply": "Reads the first result for a topic.", "ready": True, "document": DOCUMENT}
)


WANTED = "Search the web for a topic and fetch the first result"


class Drafter:
    """The drafting model: a script of replies, or a function of the call."""

    name = "drafter"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((messages, timeout))
        reply = self.replies.pop(0) if self.replies else READY
        text = reply() if callable(reply) else reply
        return Completion(text=text, model=model, provider="drafter")


class Gate:
    def __init__(self, checks=None, tools=TOOLS):
        self.checks, self.checked, self._tools = list(checks or []), [], tools

    def tools(self):
        if self._tools is None:
            raise ToolGateUnavailable("ConnectError")
        return list(self._tools)

    def check_document(self, document):
        self.checked.append(document)
        found = self.checks.pop(0) if self.checks else {"valid": True, "issues": []}
        if isinstance(found, Exception):
            raise found
        return found


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def configure(store, **drafting):
    settings = config()
    if drafting:
        settings["roleSettings"]["roles"]["drafting"] = {
            **settings["roleSettings"]["roles"]["drafting"],
            **drafting,
        }
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(settings)
        ),
    )


def build(store, replies, gate=None, *, configured=True, **drafting):
    if configured:
        configure(store, **drafting)
    drafter = Drafter(replies)
    router = Router(
        local_provider=Scripted([]), local_model="test-model", providers={"one": drafter}
    )
    return Loop(store, router, toolgate=gate if gate is not None else Gate()), drafter


def shown(drafter, call=0):
    """What the drafting model was given on one call."""
    return json.loads(drafter.calls[call][0][-1].content)
