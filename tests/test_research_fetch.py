from contextlib import closing
import json

import pytest
from test_tool_turns import build
from test_web_research import SearchGate, SEARCH
from test_deep_research import PLAN

from pi import research
from pi.loop import ActedWithoutReply, TurnFailed
from pi.store import Store
from pi.toolgate import Tool, ToolResult, ToolGateUnavailable

HANDLE = "rr_current_turn_source_01"
FETCH = json.dumps({"tool": "research.fetch", "args": {"result_id": HANDLE, "max_chars": 3000}})


class FetchGate(SearchGate):
    def tools(self):
        return super().tools() + [
            Tool(
                "research.fetch",
                "Read source",
                "Read a retrieved source",
                [{"name": "result_id", "type": "string"}],
            )
        ]

    def invoke(self, tool_id, args, approval_request_id=None, **kwargs):
        if tool_id == "research.fetch":
            self.invocations.append((tool_id, args, approval_request_id))
            return ToolResult(
                True,
                {
                    "result_id": args["result_id"],
                    "url": "https://example.org/source",
                    "text": "Bounded source excerpt",
                    "truncated": True,
                },
                tool_id,
            )
        result = super().invoke(tool_id, args, approval_request_id, **kwargs)
        if isinstance(result, ToolResult):
            result.result["results"][0]["result_id"] = HANDLE
        return result


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_deep_fetches_current_source_and_synthesis_sees_read_receipt(store):
    gate = FetchGate()
    loop, provider = build(store, [PLAN, SEARCH, FETCH, "Sourced answer"], gate)
    result = loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert [call[0] for call in gate.invocations] == ["research.web", "research.fetch"]
    assert any("Bounded source excerpt" in m.content for m in provider.sent[-1])
    receipt = research.receipt(store, result["turn_id"])
    assert receipt["actions"][1]["source_message_id"]
    assert receipt["actions"][1]["observation"]["result"]["truncated"]


@pytest.mark.parametrize(
    "args",
    [
        {"result_id": "rr_foreign_turn_source_01"},
        {"result_id": HANDLE, "url": "http://localhost/admin"},
        {"result_id": HANDLE, "max_chars": 20000},
        {"result_id": HANDLE, "max_chars": True},
    ],
)
def test_fetch_rejects_foreign_handles_urls_and_unbounded_inputs(store, args):
    gate = FetchGate()
    loop, _ = build(
        store, [PLAN, SEARCH, json.dumps({"tool": "research.fetch", "args": args})], gate
    )
    with pytest.raises(ActedWithoutReply):
        loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert [call[0] for call in gate.invocations] == ["research.web"]


def test_fetch_cannot_precede_search_or_escape_web_mode(store):
    for mode, replies in (("deep", [PLAN, FETCH]), ("web", [FETCH])):
        gate = FetchGate()
        loop, _ = build(store, replies, gate)
        with pytest.raises(TurnFailed):
            loop.run_turn(store.create_session(), "Investigate", research_mode=mode)
        assert gate.invocations == []


def test_reads_share_the_original_action_ceiling(store):
    gate = FetchGate()
    loop, _ = build(store, [PLAN, SEARCH, FETCH, SEARCH], gate)
    loop.max_tool_steps = 2
    with pytest.raises(ActedWithoutReply) as failed:
        loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert len(gate.invocations) == 2
    assert research.receipt(store, failed.value.turn_id)["action_limit"] == 2


def test_another_conversations_real_source_handle_is_not_authority(store):
    first, _ = build(store, [SEARCH, "First answer"], FetchGate())
    first.run_turn(store.create_session(), "First search", research_mode="web")
    gate = FetchGate()
    invoke = gate.invoke

    def no_handles(*args, **kwargs):
        result = invoke(*args, **kwargs)
        if args[0] == "research.web":
            result.result["results"][0].pop("result_id")
        return result

    gate.invoke = no_handles
    second, _ = build(store, [PLAN, SEARCH, FETCH], gate)
    with pytest.raises(ActedWithoutReply):
        second.run_turn(store.create_session(), "Other search", research_mode="deep")
    assert [call[0] for call in gate.invocations] == ["research.web"]


def test_lost_fetch_receipt_is_reconciled_without_reading_again(store):
    gate = FetchGate()
    invoke, saved = gate.invoke, {}

    def lost(tool_id, *args, **kwargs):
        result = invoke(tool_id, *args, **kwargs)
        if tool_id == "research.fetch":
            saved[kwargs["action_id"]] = result
            raise ToolGateUnavailable("lost read receipt")
        return result

    gate.invoke = lost
    gate.check_action = lambda action_id, tool_id: saved[action_id]
    loop, _ = build(store, [PLAN, SEARCH, FETCH, "Sourced answer"], gate)
    result = loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert result["status"] == "outcome_unknown"
    loop.resume_turn(result["turn_id"])
    assert len(gate.invocations) == 2
    assert research.receipt(store, result["turn_id"])["actions"][1]["state"] == "completed"
