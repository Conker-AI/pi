"""The owner's research settings only ever lower research's fixed limits, and only when set."""

import json
from contextlib import closing

import pytest
from test_deep_research import PLAN
from test_research_fetch import FETCH, HANDLE, FetchGate
from test_tool_turns import build
from test_web_research import SEARCH, SearchGate

from pi.actions.tools import ToolCall
from pi.context import research
from pi.conversation.store import Store
from pi.owner import settings as owner_settings


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    current = owner_settings.load(store)
    owner_settings.save(
        store, owner_settings.Update(expected_revision=current["revision"], values=values)
    )


def search(store, **args):
    """The arguments one research.web call goes out with, after Pi holds it to the settings."""
    call = ToolCall(research.WEB_TOOL, {"query": "memory database design", **args})
    research.validate_call(call, store)
    return call.args


@pytest.mark.parametrize(
    ("chosen", "limit"),
    [(None, 4), (2, 2), (1, 1), (10, 4), (50, 4), (0, 1), (2.7, 2), ("3", 4), (True, 4)],
)
def test_searches_in_deep_research_lower_the_four_step_ceiling(store, chosen, limit):
    if chosen is not None:
        choose(store, {research.DEEP_SEARCHES: chosen})
    assert research.deep_search_limit(store, 4) == limit
    assert research.deep_search_limit(store, 1) == 1


def test_a_deep_research_stops_at_the_owners_searches_and_keeps_them_with_its_plan(store):
    choose(store, {research.DEEP_SEARCHES: 1})
    gate = SearchGate()
    loop, provider = build(store, [PLAN, SEARCH, "Synthesis from one search"], gate)
    result = loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert len(gate.invocations) == 1
    assert any("limit is reached" in m.content for m in provider.sent[-1])
    choose(store, {research.DEEP_SEARCHES: 3})
    receipt = research.receipt(store, result["turn_id"])
    assert receipt["search_limit"] == receipt["action_limit"] == 1


@pytest.mark.parametrize(
    ("chosen", "asked", "sent"),
    [
        (3, {"max_results": 5}, 3),
        (3, {}, 3),
        (3, {"max_results": 2}, 2),
        (10, {"max_results": 5}, 5),
        (10, {}, 8),
        (1, {"max_results": 8}, 1),
        (-4, {"max_results": 8}, 1),
    ],
)
def test_results_per_search_hold_a_search_to_the_owners_number(store, chosen, asked, sent):
    choose(store, {research.RESULTS: chosen})
    assert search(store, **asked)["max_results"] == sent


@pytest.mark.parametrize(
    ("chosen", "asked", "sent"),
    [
        ("month", {"recency_days": 365}, 30),
        ("month", {}, 30),
        ("month", {"recency_days": 7}, 7),
        ("year", {"recency_days": 3650}, 365),
        ("year", {}, 365),
        ("any", {"recency_days": 3650}, 3650),
    ],
)
def test_prefer_recent_holds_a_search_to_the_owners_age(store, chosen, asked, sent):
    choose(store, {research.RECENT: chosen})
    assert search(store, **asked)["recency_days"] == sent


@pytest.mark.parametrize(
    "values",
    [
        {},
        {research.RECENT: "any"},
        {research.RECENT: "week", research.RESULTS: "five"},
        {research.RECENT: ["month"], research.RESULTS: None},
        {research.RESULTS: False},
    ],
)
def test_unset_or_malformed_settings_leave_a_search_as_the_model_asked(store, values):
    choose(store, values)
    assert search(store) == {"query": "memory database design"}
    assert search(store, max_results=5, recency_days=90) == {
        "query": "memory database design",
        "max_results": 5,
        "recency_days": 90,
    }


def test_the_bare_contract_check_changes_nothing_and_the_settings_raise_no_limit(store):
    choose(store, {research.RESULTS: 3, research.RECENT: "month"})
    call = ToolCall(research.WEB_TOOL, {"query": "memory database design", "max_results": 5})
    research.validate_call(call)
    assert call.args == {"query": "memory database design", "max_results": 5}
    choose(store, {research.RESULTS: 50})
    for bad in ({"max_results": 9}, {"recency_days": 4000}):
        with pytest.raises(RuntimeError, match="bounded input contract"):
            search(store, **bad)


def test_a_web_research_turn_sends_and_records_the_held_search(store):
    choose(store, {research.RESULTS: 3, research.RECENT: "year"})
    gate = SearchGate()
    loop, _ = build(store, [SEARCH, "Summary: https://example.org/source"], gate)
    result = loop.run_turn(store.create_session(), "Research databases", research_mode="web")
    [(tool_id, args, _)] = gate.invocations
    assert tool_id == research.WEB_TOOL
    assert args == {"query": "memory database design", "max_results": 3, "recency_days": 365}
    [action] = research.receipt(store, result["turn_id"])["actions"]
    assert action["arguments"] == args


@pytest.mark.parametrize(
    ("chosen", "sent"), [(None, 3000), (2000, 2000), (20000, 3000), (500, 1000), ("big", 3000)]
)
def test_web_page_size_holds_a_page_read_under_pis_cap(store, chosen, sent):
    if chosen is not None:
        choose(store, {research.PAGE_LIMIT: chosen})
    gate = FetchGate()
    loop, _ = build(store, [PLAN, SEARCH, FETCH, "Sourced answer"], gate)
    loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert [call[0] for call in gate.invocations] == [research.WEB_TOOL, research.FETCH_TOOL]
    assert gate.invocations[1][1]["max_chars"] == sent


def test_a_page_read_the_model_left_unsized_gets_the_owners_size(store):
    choose(store, {research.PAGE_LIMIT: 4000})
    unsized = json.dumps({"tool": research.FETCH_TOOL, "args": {"result_id": HANDLE}})
    gate = FetchGate()
    loop, _ = build(store, [PLAN, SEARCH, unsized, "Sourced answer"], gate)
    loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert gate.invocations[1][1] == {"result_id": HANDLE, "max_chars": 4000}
