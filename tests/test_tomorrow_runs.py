"""GET /insights/tomorrow `runs` (R-U10): each start Conker makes on its own in the next day, and
whether it would run, by the runner's own checks. The last test replays the day through the
runner with a fixed clock and finds the same decision at every slot."""

from __future__ import annotations

import random
from contextlib import closing

import pytest
from test_agent_jobs import NIGHT, NOON, Loop, ai_job
from test_owner_setting_readers_jobs import choose

from pi.agents import tomorrow
from pi.conversation.store import Store
from pi.core import owner_preferences as prefs
from pi.schedule import agent_jobs, jobs, proposals

HOUR = 3600
# NOON is 12:00 in Jerusalem; the default quiet hours (22:00-07:00) start ten hours later.
QUIET_FROM, QUIET_UNTIL = NOON + 10 * HOUR, NOON + 19 * HOUR


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def runs(store, now=NOON, **options):
    page = tomorrow.tomorrow(store, now, 24, **options)
    tomorrow.Tomorrow.model_validate(page)
    return page["runs"]


def of(items, job):
    return [item for item in items if item["jobId"] == job["id"]]


def set_preferences(store, **changes):
    current = prefs.load(store)
    preferences = {**current["preferences"], **changes}
    prefs.save(
        store,
        prefs.UpdatePreferences(expected_revision=current["revision"], preferences=preferences),
    )


def test_a_repeating_job_in_quiet_hours_is_held_and_a_timed_one_runs(store):
    repeating = agent_jobs.create(store, ai_job(), now=NOON)
    timed = agent_jobs.create(
        store,
        ai_job(name="Late review", timing=dict(kind="daily", time="02:00", day=0, hours=1)),
        now=NOON,
    )
    items = runs(store)
    for item in of(items, repeating):
        quiet = QUIET_FROM <= item["at"] < QUIET_UNTIL
        assert (item["wouldRun"], item["reason"]) == (not quiet, "QUIET_HOURS" if quiet else None)
    assert sum(not item["wouldRun"] for item in of(items, repeating)) == 5  # 22:00 to 06:00
    [late] = of(items, timed)
    assert late["wouldRun"] and QUIET_FROM < late["at"] < QUIET_UNTIL
    assert all(item["kind"] == "clock" for item in items)
    assert [item["at"] for item in items] == sorted(item["at"] for item in items)


def test_a_job_over_its_runs_a_day_is_held(store):
    choose(store, {"proactivity.jobs.max_runs": 2})
    job = agent_jobs.create(store, ai_job(), now=NOON)
    items = [item for item in of(runs(store), job) if item["reason"] != "QUIET_HOURS"]
    assert [item["wouldRun"] for item in items[:3]] == [True, True, False]
    assert {item["reason"] for item in items[2:]} == {"RUN_LIMIT"}


def test_conker_jobs_share_the_daily_budget_and_follow_proactivity(store):
    set_preferences(store, dailyBudget={"suggestions": 3, "researchMinutes": 0, "costCents": 0})
    job = agent_jobs.create(store, ai_job(), now=NOON, created_by="conker")
    mine = agent_jobs.create(store, ai_job(name="Mine"), now=NOON)
    held = [item["reason"] for item in of(runs(store), job)]
    assert held[:4] == [None, None, None, "OVER_BUDGET"]
    assert not any(item["reason"] for item in of(runs(store), mine)[:4])
    set_preferences(store, urgency="off")
    reasons = {item["reason"] for item in of(runs(store), job)}
    assert reasons == {"PROACTIVITY_OFF", "QUIET_HOURS"}  # the clock skips quiet hours first


def test_watches_are_listed_as_checks_and_a_trigger_he_left_out_holds(store):
    watch = agent_jobs.create(
        store, ai_job(name="Watcher", watch=dict(toolId="t_get", everyMinutes=15)), now=NOON
    )
    [item] = runs(store)
    assert (item["kind"], item["at"], item["everyMinutes"], item["wouldRun"]) == (
        "watch",
        None,
        15,
        True,
    )
    choose(store, {"proactivity.jobs.triggers": ["time"], "proactivity.jobs.min_interval": 30})
    [item] = runs(store)
    assert (item["jobId"], item["everyMinutes"], item["reason"]) == (watch["id"], 30, "TRIGGER_OFF")


def test_without_the_job_worker_nothing_runs(store):
    agent_jobs.create(store, ai_job(), now=NOON)
    items = runs(store, scheduler=False)
    assert items and {item["reason"] for item in items} == {"SCHEDULER_OFF"}


class Worker:
    interval_seconds, check_seconds = 24 * HOUR, HOUR


def test_the_ideas_pass_waits_out_quiet_hours_and_needs_its_role(store, monkeypatch):
    [item] = runs(store, now=NIGHT, proposal_worker=Worker())
    assert (item["kind"], item["at"], item["reason"]) == ("proposals", NIGHT, "ROLE_OFF")
    monkeypatch.setattr(proposals, "model_call", lambda store, router: (True, None))
    held, ran = runs(store, now=NIGHT, proposal_worker=Worker())
    assert (held["at"], held["reason"]) == (NIGHT, "QUIET_HOURS")
    assert (ran["at"], ran["wouldRun"]) == (NIGHT + 5 * HOUR, True)  # 07:00 in Jerusalem
    assert runs(store) == []  # no worker, no pass


def scenario(store, seed):
    """A random owner: his limits and preferences, and a few AI jobs, his and Conker's."""
    rng = random.Random(seed)
    set_preferences(
        store,
        urgency=rng.choice(["meaningful", "meaningful", "urgent_only", "off"]),
        dailyBudget={"suggestions": rng.randint(0, 6), "researchMinutes": 0, "costCents": 0},
        quietHours={**prefs.DEFAULT["quietHours"], "enabled": rng.random() < 0.7},
    )
    settings = {"proactivity.jobs.max_runs": rng.randint(1, 8)}
    if rng.random() < 0.5:
        settings["proactivity.jobs.min_interval"] = rng.choice([5, 90, 200])
    if rng.random() < 0.15:
        settings["proactivity.jobs.triggers"] = ["watch"]
    choose(store, settings)
    for index in range(rng.randint(1, 4)):
        if rng.random() < 0.7:
            timing = dict(kind="interval", time="00:00", day=0, hours=rng.choice([1, 2, 3, 5]))
        else:
            timing = dict(kind="daily", time=f"{rng.randint(0, 23):02d}:00", day=0, hours=1)
        by = rng.choice(["owner", "conker"])
        agent_jobs.create(
            store, ai_job(name=f"Job {index}", timing=timing), now=NOON, created_by=by
        )


@pytest.mark.parametrize("seed", range(12))
def test_the_forecast_is_what_the_runner_decides_then(store, seed):
    scenario(store, seed)
    items = runs(store)
    expected = {(item["at"], item["jobId"]): item for item in items}
    executor = agent_jobs.AgentJobs(store, Loop({"message": {"content": "NOTHING"}}))
    for at in sorted({item["at"] for item in items}):
        claimed = {run["job_id"]: run for run in jobs.claim_due(store, now=at, limit=100)}
        for (moment, job_id), item in expected.items():
            if moment != at:
                continue
            run = claimed.pop(job_id, None)
            if run is None:  # the clock did not start it: only quiet hours do that here
                assert item["reason"] == "QUIET_HOURS", item
                continue
            status = jobs.dispatch_claim(
                store,
                run,
                lambda target, action_id, at=at, **_: executor.run(action_id, now=at),
            )
            code = (jobs.get_run(store, run["id"])["receipt"] or {}).get("code")
            assert (status == "completed", code) == (item["wouldRun"], item["reason"]), item
        assert claimed == {}  # the runner started nothing the forecast left out
