"""A run the owner's own limits hold back before the model is recorded as skipped, not failed."""

import json
from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_morning_checkin import LATE, NOON
from test_tool_turns import FakeGate, build

from pi.agents import continuity, inbox
from pi.conversation.store import Store
from pi.core import owner_preferences as prefs
from pi.schedule import agent_jobs, job_views, jobs, proactive_budget, rhythm


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "held.db")) as store:
        yield store


def proactivity_off(store):
    current = prefs.load(store)
    preferences = {**current["preferences"], "urgency": "off"}
    prefs.save(
        store,
        prefs.UpdatePreferences(expected_revision=current["revision"], preferences=preferences),
    )


def budget_used_up(store):
    instant = datetime.fromtimestamp(NOON, UTC)
    for index in range(prefs.DEFAULT["dailyBudget"]["suggestions"]):
        proactive_budget.reserve(
            store,
            proactive_budget.Request(
                request_id=f"other-proactive-work-{index:04d}",
                expected_revision=proactive_budget.status(store, now=instant)["preferenceRevision"],
                proposed=prefs.Usage(suggestions=1, researchMinutes=0, costCents=0),
            ),
            now=instant,
        )


# Each of the owner's limits, how to set it up, when the run starts and the code it records.
HELD = [
    ("QUIET_HOURS", lambda store: None, LATE),
    ("OVER_BUDGET", budget_used_up, NOON),
    ("PROACTIVITY_OFF", proactivity_off, NOON),
]


def dispatch(store, run_id, now):
    """One check-in run through the worker's own path; its stored run and the model's turns."""
    loop, _ = build(store, ["Something for you"], FakeGate())
    turns = []
    real = loop.run_turn
    loop.run_turn = lambda *args, **kwargs: turns.append(1) or real(*args, **kwargs)
    run = jobs.run_now(store, rhythm.MORNING_CHECK_IN, run_id, now=now)
    executor = agent_jobs.AgentJobs(store, loop)
    status = jobs.dispatch_claim(
        store, {"id": run["id"]}, lambda *a, **k: executor.run(run["id"], now)
    )
    return status, jobs.get_run(store, run["id"]), len(turns)


@pytest.mark.parametrize(("code", "setup", "now"), HELD)
def test_a_held_back_run_is_skipped_with_its_reason(store, code, setup, now):
    rhythm.seed(store, now=NOON)
    setup(store)
    status, run, turns = dispatch(store, "held-back-run-0001", now)
    assert status == run["status"] == "skipped" and turns == 0
    assert run["receipt"]["code"] == code and run["receipt"]["reason"].startswith("Held back: ")
    view = job_views.browser_run(run)
    assert (view.status, view.outcomeCode, view.execution) == ("skipped", code, "resolved")


@pytest.mark.parametrize(("code", "setup", "now"), HELD)
def test_a_skipped_run_needs_nothing_from_the_owner(store, code, setup, now):
    rhythm.seed(store, now=NOON)
    setup(store)
    dispatch(store, "held-back-run-0002", now)
    [item] = [
        entry for entry in continuity.briefing(store)["items"] if entry["kind"] == "job_status"
    ]
    assert item["status"] == "skipped" and item["needsAttention"] is False
    # What the next check-in reads: no Inbox card says a job failed or a part is broken.
    needs = inbox.inbox(store, now=now)["needsYou"]
    assert [entry for entry in needs if entry["kind"] in ("attention", "health")] == []


def test_a_skipped_run_does_not_hold_the_next_one_back(store):
    rhythm.seed(store, now=NOON)
    dispatch(store, "held-back-run-0003", LATE)
    status, run, turns = dispatch(store, "held-back-run-0004", NOON)
    assert status == run["status"] == "completed" and turns == 1


def test_a_tool_receipt_cannot_claim_skipped(store):
    rhythm.seed(store, now=NOON)
    job = jobs.get_job(store, rhythm.MORNING_CHECK_IN)
    run = jobs.run_now(store, job["id"], "held-back-run-0005", now=NOON)
    with store._connect() as db:
        target = {"kind": "tool", "id": "x", "publishedVersion": 1, "digest": "0" * 64}
        db.execute(
            "UPDATE scheduled_runs SET definition=json_set(definition,'$.target',json(?)) WHERE id=?",
            (json.dumps(target), run["id"]),
        )
        db.commit()
    status = jobs.dispatch_claim(store, {"id": run["id"]}, lambda *a, **k: {"status": "skipped"})
    assert status == "outcome_unknown"
