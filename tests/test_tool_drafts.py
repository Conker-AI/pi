"""Conker's tool requests are filed at once and drafted in the background, checked and repaired."""

import json

import pytest

from pi.actions.toolgate import ToolGateUnavailable
from pi.agents import change_requests, tool_drafts
from pi.conversation.store import Store
from pi.models.routing import Router
from pi.turns import tool_drafting
from pi.turns.loop import Loop
from tests.test_tool_turns import Scripted
from tests.tool_drafts_helpers import (
    DOCUMENT,
    READY,
    TOOLS,
    WANTED,
    Gate,
    build,
    shown,
    store,  # noqa: F401 -- shared fixture
)


def test_a_chat_turn_files_the_request_and_answers_without_waiting(store):
    call = json.dumps({"tool": "conker.draft_tool", "args": {"description": WANTED}})
    provider = Scripted([call, "I'm drafting it; it will be in your Inbox."])
    gate = Gate(tools=TOOLS[:3])
    loop = Loop(store, Router(local_provider=provider, local_model="m"), toolgate=gate)
    session = store.create_session()
    result = loop.run_turn(session, "Make me a tool that " + WANTED.lower())
    assert result["message"]["content"] == "I'm drafting it; it will be in your Inbox."
    # Two model calls only: the request and the answer. No reviewer, no drafting in the turn.
    assert len(provider.sent) == 2
    [request] = change_requests.listing(store)
    assert request["kind"] == "tool_draft" and request["sessionId"] == session
    assert request["payload"] == {"description": WANTED, "status": "drafting"}
    assert request["title"] == "Draft a tool: " + WANTED
    told = [m.content for m in provider.sent[1] if m.role == "tool"][-1]
    assert '"status": "drafting"' in told and "Inbox" in told
    assert loop.drafts._wake.is_set()  # the worker was woken


def test_the_tool_says_when_to_use_it():
    from pi.turns import native_tools

    [tool] = [t for t in native_tools.CATALOG if t.id == native_tools.DRAFT_TOOL]
    assert "whenever the owner asks for a new tool" in tool.description
    assert "instead of doing the job yourself" in tool.description


def test_the_worker_drafts_a_ready_card_with_plain_steps(store):
    gate = Gate()
    loop, drafter = build(store, [READY], gate)
    filed = tool_drafts.file(store, WANTED)
    assert loop.drafts.tick() == 1
    request = change_requests.get(store, filed["id"])
    payload = request["payload"]
    assert payload["status"] == "ready" and payload["checked"] is True and "note" not in payload
    assert payload["description"] == WANTED and payload["issues"] == []
    assert payload["document"]["id"] == "topic-reader" and payload["document"]["kind"] == "workflow"
    assert payload["steps"] == [
        "Start with the topic",
        "Search the web for the topic (uses Web search)",
        "Fetch the first result (uses Fetch page)",
        "Give back the previous step's result",
    ]
    assert request["state"] == "open" and request["title"] == "Add the tool “Topic reader”"
    assert request["summary"] == "Reads the first result for a topic."
    assert gate.checked == [payload["document"]]
    # Each model call gets the role's own limit (1 s in these settings), not ten minutes.
    assert [timeout for _, timeout in drafter.calls] == [1.0]
    assert tool_drafts.pending(store) == []


def test_only_matching_tools_are_offered_and_workflows_are_building_blocks(store):
    loop, drafter = build(store, [READY])
    tool_drafts.file(store, WANTED + ", then add my daily brief")
    loop.drafts.tick()
    offered = {item["id"]: item for item in shown(drafter)["capabilities"]}
    assert set(offered) == {"web.search", "web.fetch", "daily-brief"}
    assert offered["daily-brief"]["kind"] == "workflow" and offered["daily-brief"]["version"] == 3
    assert "authorization" not in offered["web.search"]


def test_the_drafting_role_drafts_and_the_answer_role_is_the_fallback(store):
    # Model b's provider is not connected here, so the drafting role falls back to model a, with
    # the drafting role's own time limit (the answer role has 1 s).
    loop, drafter = build(
        store,
        [READY],
        enabled=True,
        eligibleModelIds=["a", "b"],
        modelId="b",
        timeoutMs=5000,
        failure="fallback",
        fallbackModelId="a",
    )
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    [(_, timeout)] = drafter.calls
    assert timeout == 5.0
    assert change_requests.get(store, filed["id"])["payload"]["status"] == "ready"


def test_checked_issues_are_repaired_at_most_twice(store):
    issue = {"valid": False, "issues": [{"code": "graph", "message": "No path", "nodeId": "x"}]}
    gate = Gate([issue, issue, issue])
    loop, drafter = build(store, [READY, READY, READY, READY], gate)
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    payload = change_requests.get(store, filed["id"])["payload"]
    assert len(drafter.calls) == 3 and len(gate.checked) == 3
    assert shown(drafter, 1)["issues"] == ["Step x: No path"] and shown(drafter, 1)["previous"]
    assert payload["status"] == "ready" and payload["issues"] == ["Step x: No path"]


def test_a_repair_that_passes_the_check_stops_repairing(store):
    issue = {"valid": False, "issues": [{"code": "graph", "message": "No path", "nodeId": None}]}
    gate = Gate([issue])
    loop, drafter = build(store, [READY, READY], gate)
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    payload = change_requests.get(store, filed["id"])["payload"]
    assert len(drafter.calls) == 2 and payload["issues"] == [] and payload["checked"] is True


class Older:
    """A ToolGate from before the document check."""

    def tools(self):
        return list(TOOLS)


@pytest.mark.parametrize(
    "gate", [lambda: Gate([ToolGateUnavailable("check_404")]), Older], ids=["unreachable", "older"]
)
def test_without_toolgates_check_the_draft_is_ready_with_pis_own_issues(store, gate):
    gate = gate()
    unknown = json.loads(json.dumps(DOCUMENT))
    unknown["nodes"][2]["config"]["tool"] = "bank.transfer"
    loop, drafter = build(
        store, [json.dumps({"reply": "Reads.", "ready": True, "document": unknown})], gate
    )
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    payload = change_requests.get(store, filed["id"])["payload"]
    assert payload["status"] == "ready" and payload["checked"] is False
    assert payload["note"] == tool_drafts.NOT_CHECKED
    assert payload["issues"] == ["Step fetch uses 'bank.transfer', which is not an available tool."]
    assert len(drafter.calls) == 1


def test_a_question_from_the_drafter_fails_the_card_with_its_words(store):
    asked = json.dumps({"reply": "Which search engine?", "ready": False, "document": None})
    loop, _ = build(store, [asked])
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    assert change_requests.get(store, filed["id"])["payload"] == {
        "description": WANTED,
        "status": "failed",
        "error": "The drafter needs more detail: Which search engine?",
    }


def test_no_model_no_toolgate_and_no_tools_fail_in_plain_words(store, tmp_path):
    loop, _ = build(store, [], configured=False)
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    assert (
        "Choose one in Models settings"
        in change_requests.get(store, filed["id"])["payload"]["error"]
    )
    other = Store(tmp_path / "other.db")
    loop, _ = build(other, [], Gate(tools=None))
    filed = tool_drafts.file(other, WANTED)
    loop.drafts.tick()
    assert change_requests.get(other, filed["id"])["payload"]["error"] == tool_drafting.NO_TOOLGATE
    loop.toolgate = None
    again = tool_drafts.file(other, WANTED)
    loop.drafts.tick()
    assert change_requests.get(other, again["id"])["payload"]["error"] == tool_drafting.NO_TOOLS
