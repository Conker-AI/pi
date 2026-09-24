"""Deep research plans once and adapts bounded searches from retained evidence."""

from contextlib import closing

import pytest
from test_tool_turns import build
from test_web_research import SearchGate, SEARCH
from test_forgetting import erase

from pi import research, tasks, turn_control
from pi.loop import ActedWithoutReply, TurnFailed
from pi.store import Store

PLAN = '{"research_plan":["Find primary sources", "Resolve contradictory evidence"]}'
FOLLOWUP = '{"tool":"research.web","args":{"query":"follow up from first source","max_results":3}}'


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_plan_and_adaptive_searches_are_inspectable_after_reopen(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        gate = SearchGate()
        loop, provider = build(store, [PLAN, SEARCH, FOLLOWUP, "Synthesis with limitations"], gate)
        result = loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
        tid = result["turn_id"]
        assert len(gate.invocations) == 2
        assert any("Untrusted text" in m.content for m in provider.sent[2])
        assert gate.invocations[1][1]["query"] == "follow up from first source"
    with closing(Store(path)) as store:
        receipt = research.receipt(store, tid)
        assert receipt["status"] == "complete" and receipt["search_limit"] == 4
        assert receipt["plan"]["questions"] == [
            "Find primary sources",
            "Resolve contradictory evidence",
        ]
        assert len(receipt["actions"]) == 2
        assert all(action["source_message_id"] for action in receipt["actions"])


@pytest.mark.parametrize("bad", ["not json", '{"research_plan":[]}', '{"research_plan":[42]}'])
def test_invalid_plan_stops_before_search(store, bad):
    gate = SearchGate()
    loop, _ = build(store, [bad], gate)
    with pytest.raises(TurnFailed):
        loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert gate.invocations == []


def test_search_limit_is_hard_and_recovery_only_narrates(store):
    gate = SearchGate()
    loop, provider = build(store, [PLAN, SEARCH, FOLLOWUP, "Partial synthesis"], gate)
    loop.max_tool_steps = 1
    with pytest.raises(ActedWithoutReply) as failed:
        loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert len(gate.invocations) == 1
    assert any("limit is reached" in m.content for m in provider.sent[-1])
    loop.resume_turn(failed.value.turn_id)
    assert len(gate.invocations) == 1
    assert research.receipt(store, failed.value.turn_id)["search_limit"] == 1


def test_approval_resume_keeps_plan_and_continues_from_saved_evidence(store):
    gate = SearchGate(needs_approval=True)
    loop, provider = build(store, [PLAN, SEARCH, FOLLOWUP, "Final synthesis"], gate)
    first = loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert first["status"] == "awaiting_approval"
    saved_plan = research.plan(store, first["turn_id"])
    second = loop.resume_turn(first["turn_id"])
    assert second["status"] == "awaiting_approval"
    loop.resume_turn(first["turn_id"])
    assert research.plan(store, first["turn_id"]) == saved_plan
    assert len(provider.sent) == 4
    assert len(research.receipt(store, first["turn_id"])["actions"]) == 2


def test_partial_failure_can_inform_next_search_without_erasing_receipt(store):
    gate = SearchGate(refuse=("SOURCE_UNAVAILABLE", "Source temporarily unavailable"))
    loop, provider = build(store, [PLAN, SEARCH, FOLLOWUP, "Evidence remains insufficient"], gate)
    result = loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    receipt = research.receipt(store, result["turn_id"])
    assert len(receipt["actions"]) == 2
    assert all(action["state"] == "refused" for action in receipt["actions"])
    assert any("SOURCE_UNAVAILABLE" in m.content for m in provider.sent[2])


def test_forgetting_removes_public_plan_and_blocks_receipt(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        loop, _ = build(store, [PLAN, SEARCH, "Answer"], SearchGate())
        tid = loop.run_turn(sid, "Investigate", research_mode="deep")["turn_id"]
        assert research.plan(store, tid)
    erase(path, sid)
    with closing(Store(path)) as store:
        assert research.plan(store, tid) is None
        with pytest.raises(tasks.TaskError):
            research.receipt(store, tid)


def test_stop_during_plan_does_not_save_plan_or_run_search(store):
    gate = SearchGate()
    loop, provider = build(store, [PLAN], gate)
    complete = provider.complete

    def stop(messages, *, model):
        with store._connect() as db:
            tid = db.execute("SELECT id FROM turns WHERE status='running'").fetchone()[0]
        turn_control.cancel(store, tid)
        return complete(messages, model=model)

    provider.complete = stop
    with pytest.raises(TurnFailed) as stopped:
        loop.run_turn(store.create_session(), "Investigate", research_mode="deep")
    assert research.plan(store, stopped.value.turn_id) is None
    assert gate.invocations == []
