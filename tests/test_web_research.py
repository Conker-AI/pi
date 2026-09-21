"""Bounded web execution through existing ToolGate actions, never a parallel search client."""

from contextlib import closing

import pytest
from test_tool_turns import FakeGate, build

from pi import research, turn_control
from pi.loop import ActedWithoutReply, TurnFailed
from pi.providers import ProviderUnavailable
from pi.store import Store
from pi.toolgate import Tool, ToolResult, ToolGateUnavailable

SEARCH = '{"tool":"research.web","args":{"query":"memory database design","max_results":5}}'


class SearchGate(FakeGate):
    def tools(self):
        return super().tools() + [Tool("research.web", "Search", "Search the web", [{"name": "query", "type": "string"}])]

    def invoke(self, tool_id, args, approval_request_id=None, **kwargs):
        result = super().invoke(tool_id, args, approval_request_id, **kwargs)
        if not isinstance(result, ToolResult):
            return result
        return ToolResult(True, {"query": args["query"], "provider": "test-provider",
            "results": [{"url": "https://example.org/source", "title": "Source", "snippet": "Untrusted text"}]}, tool_id)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_web_runs_once_exposes_sources_and_replay_does_not_search(store):
    gate = SearchGate()
    loop, provider = build(store, [SEARCH, "Summary: https://example.org/source"], gate)
    sid = store.create_session()
    result = loop.run_turn(sid, "Research databases", request_id="web_request_0001", research_mode="web")
    assert len(gate.invocations) == 1 and len(provider.sent) == 2
    assert not any("t_echo" in message.content for message in provider.sent[0])
    assert any("untrusted data" in message.content for message in provider.sent[1])
    receipt = research.receipt(store, result["turn_id"])
    assert receipt["status"] == "complete" and receipt["search_limit"] == 1
    assert receipt["actions"][0]["source_message_id"]
    observation = receipt["actions"][0]["observation"]
    assert observation["result"]["results"][0]["url"] == "https://example.org/source"
    assert loop.run_turn(sid, "Research databases", request_id="web_request_0001", research_mode="web")["replayed"]
    assert len(gate.invocations) == 1


@pytest.mark.parametrize("answer", ["Unsourced answer", '{"tool":"t_echo","args":{"text":"leak"}}',
    '{"tool":"research.web","args":{"query":"x"}}',
    '{"tool":"research.web","args":{"query":"valid query","max_results":999}}'])
def test_missing_or_invalid_search_cannot_be_saved_as_completed_research(store, answer):
    gate = SearchGate()
    loop, _ = build(store, [answer], gate)
    with pytest.raises(TurnFailed):
        loop.run_turn(store.create_session(), "Search", research_mode="web")
    assert gate.invocations == []


def test_second_search_is_not_executed_and_reply_can_be_recovered(store):
    gate = SearchGate()
    loop, _ = build(store, [SEARCH, SEARCH, "Use the saved source"], gate)
    with pytest.raises(ActedWithoutReply) as failed:
        loop.run_turn(store.create_session(), "Search", research_mode="web")
    assert len(gate.invocations) == 1
    result = loop.resume_turn(failed.value.turn_id)
    assert result["status"] == "complete" and len(gate.invocations) == 1


def test_approval_resume_uses_exact_query_without_second_search(store):
    gate = SearchGate(needs_approval=True)
    loop, _ = build(store, [SEARCH, "From the approved source"], gate)
    result = loop.run_turn(store.create_session(), "Search", research_mode="web")
    assert result["status"] == "awaiting_approval"
    resumed = loop.resume_turn(result["turn_id"])
    assert resumed["status"] == "complete"
    assert gate.invocations[0][1] == gate.invocations[1][1]
    assert gate.invocations[1][2] == result["approval"]["request_id"]
    assert len(research.receipt(store, result["turn_id"])["actions"]) == 1


def test_provider_failure_after_search_retains_sources_for_reply_only_recovery(store):
    gate = SearchGate()
    loop, _ = build(store, [SEARCH, ProviderUnavailable("offline"), "Recovered source summary"], gate)
    with pytest.raises(ActedWithoutReply) as failed:
        loop.run_turn(store.create_session(), "Search", research_mode="web")
    assert research.receipt(store, failed.value.turn_id)["actions"][0]["source_message_id"]
    loop.resume_turn(failed.value.turn_id)
    assert len(gate.invocations) == 1


def test_stop_during_search_keeps_result_but_never_calls_synthesis(store):
    gate = SearchGate()
    loop, provider = build(store, [SEARCH, "Must not be generated"], gate)
    invoke = gate.invoke
    def cancelling(*args, **kwargs):
        with store._connect() as db:
            tid = db.execute("SELECT turn_id FROM tool_actions WHERE id=?", (kwargs["action_id"],)).fetchone()[0]
        turn_control.cancel(store, tid)
        return invoke(*args, **kwargs)
    gate.invoke = cancelling
    with pytest.raises(ActedWithoutReply) as stopped:
        loop.run_turn(store.create_session(), "Search", research_mode="web")
    assert len(provider.sent) == 1
    receipt = research.receipt(store, stopped.value.turn_id)
    assert receipt["status"] == "acted_no_reply"
    assert receipt["actions"][0]["source_message_id"]


def test_lost_receipt_reconciles_without_repeating_search(store):
    gate = SearchGate()
    invoke = gate.invoke
    saved = {}
    def lost(*args, **kwargs):
        saved[kwargs["action_id"]] = invoke(*args, **kwargs)
        raise ToolGateUnavailable("lost receipt")
    gate.invoke = lost
    gate.check_action = lambda action_id, tool_id: saved[action_id]
    loop, provider = build(store, [SEARCH, "Summary from recovered receipt"], gate)
    result = loop.run_turn(store.create_session(), "Search", research_mode="web")
    assert result["status"] == "outcome_unknown"
    loop.resume_turn(result["turn_id"])
    assert len(gate.invocations) == 1
    assert any("already requested" in item.content for item in provider.sent[-1])
    assert research.receipt(store, result["turn_id"])["actions"][0]["state"] == "completed"


def test_refusal_is_retained_and_not_retried(store):
    gate = SearchGate(refuse=("SCOPE_DENIED", "Removed by owner"))
    loop, _ = build(store, [SEARCH, "Search was refused; no sources were fetched."], gate)
    result = loop.run_turn(store.create_session(), "Search", research_mode="web")
    receipt = research.receipt(store, result["turn_id"])
    assert receipt["actions"][0]["state"] == "refused"
    assert "SCOPE_DENIED" in receipt["actions"][0]["observation"]
    assert len(gate.invocations) == 1
