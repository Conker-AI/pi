"""The Morning check-in Pi ships on by default (pi/schedule/rhythm.py)."""

import asyncio
import hashlib
import os
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from test_tool_turns import FakeGate, build

from pi.api import server as api
from pi.api import startup
from pi.conversation.store import Store
from pi.owner import preferences as prefs
from pi.owner import settings
from pi.schedule import agent_jobs, jobs, rhythm
from pi.schedule.job_models import AgentJobCreate, StateChange, Timing
from pi.turns import access

# 12:00 in Jerusalem, outside the default quiet hours.
NOON = datetime(2026, 10, 29, 10, 0, tzinfo=UTC).timestamp()
OWNER = "morning-check-in-owner-key-" + "o" * 32


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "rhythm.db")) as store:
        yield store


def ai_jobs(store):
    return [job for job in jobs.list_jobs(store) if job["definition"]["target"]["kind"] == "agent"]


def test_a_fresh_store_gets_the_check_in_once(store):
    job = rhythm.seed(store, now=NOON)
    assert job["id"] == rhythm.MORNING_CHECK_IN
    definition = job["definition"]
    assert (definition["name"], definition["agentId"]) == ("Morning check-in", "companion")
    assert definition["timing"]["kind"] == "daily" and definition["timing"]["time"] == "08:00"
    assert definition["timeZone"] == agent_jobs.owner_time_zone(store) == "Asia/Jerusalem"
    assert definition["enabled"] and definition["createdBy"] == "conker"
    assert definition["instructions"].endswith("reply with only NOTHING.")
    assert agent_jobs.session_for(store, job["id"])
    assert settings.value(store, rhythm.SEEDED) == {"morningCheckIn": job["id"]}
    assert rhythm.seed(store, now=NOON) is None
    assert [item["id"] for item in ai_jobs(store)] == [job["id"]]


def test_the_seed_follows_the_owners_time_zone(store):
    current = prefs.load(store)
    chosen = {**current["preferences"]}
    chosen["quietHours"] = {**chosen["quietHours"], "timeZone": "Europe/Berlin"}
    prefs.save(
        store,
        prefs.UpdatePreferences(expected_revision=current["revision"], preferences=chosen),
    )
    assert rhythm.seed(store, now=NOON)["definition"]["timeZone"] == "Europe/Berlin"


def test_turning_it_off_or_resetting_settings_never_brings_it_back(store):
    job = rhythm.seed(store, now=NOON)
    jobs.set_enabled(store, job["id"], StateChange(expected_revision=1, enabled=False))
    assert rhythm.seed(store) is None
    # The settings document is replaced with nothing: the job itself still records the seed.
    revision = settings.load(store)["revision"]
    settings.save(store, settings.Update(expected_revision=revision, values={}))
    assert rhythm.seed(store) is None
    assert settings.value(store, rhythm.SEEDED) == {"morningCheckIn": job["id"]}
    [only] = ai_jobs(store)
    assert only["id"] == job["id"] and only["definition"]["enabled"] is False


def test_a_job_gone_from_the_table_is_not_made_again(store):
    job = rhythm.seed(store, now=NOON)
    with store._connect() as db:
        db.execute("DELETE FROM agent_job_sessions WHERE job_id=?", (job["id"],))
        db.execute("DELETE FROM scheduled_jobs WHERE id=?", (job["id"],))
    assert rhythm.seed(store) is None
    assert ai_jobs(store) == []


def test_an_existing_install_gets_it_on_upgrade_without_using_conkers_allowance(tmp_path):
    path = tmp_path / "old.db"
    timing = Timing(kind="daily", time="09:00", day=0, hours=24)
    mine = AgentJobCreate(
        name="Mine", instructions="Look at my week.", timing=timing, timeZone="Asia/Jerusalem"
    )
    with closing(Store(path)) as old:
        his = agent_jobs.create(old, mine, now=NOON)
    with closing(Store(path)) as upgraded:
        made = rhythm.seed(upgraded, now=NOON)
        assert made["id"] == rhythm.MORNING_CHECK_IN
        assert {job["id"] for job in ai_jobs(upgraded)} == {his["id"], made["id"]}
        # Conker can still make its own CONKER_PER_DAY jobs today.
        for _ in range(agent_jobs.CONKER_PER_DAY - 1):
            agent_jobs.create(upgraded, mine, now=NOON, created_by="conker")
        assert agent_jobs.room(upgraded, "conker", now=NOON) is None
        agent_jobs.create(upgraded, mine, now=NOON, created_by="conker")
        assert "already made" in agent_jobs.room(upgraded, "conker", now=NOON)


def test_a_nothing_run_files_nothing_and_is_never_the_owners_words(store):
    job = rhythm.seed(store, now=NOON)
    loop, _ = build(store, ["NOTHING"], FakeGate())
    run = jobs.run_now(store, job["id"], "morning-check-in-run-0001", now=NOON)
    receipt = agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON)
    assert receipt["status"] == "completed" and receipt["noted"] is False
    assert agent_jobs.notes(store) == []
    assert access.owner_text(store, receipt["turnId"]) == ""


def test_it_waits_while_proactivity_is_off(store):
    job = rhythm.seed(store, now=NOON)
    current = prefs.load(store)
    prefs.save(
        store,
        prefs.UpdatePreferences(
            expected_revision=current["revision"],
            preferences={**current["preferences"], "urgency": "off"},
        ),
    )
    loop, _ = build(store, ["Something for you"], FakeGate())
    run = jobs.run_now(store, job["id"], "morning-check-in-run-0002", now=NOON)
    receipt = agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON)
    assert receipt["code"] == "HELD_BACK" and "proactive work off" in receipt["reason"]
    assert jobs.get_job(store, job["id"])["definition"]["enabled"] is True


def test_the_jobs_route_lists_it_like_any_job(store, monkeypatch):
    rhythm.seed(store, now=NOON)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    digest = hashlib.sha256(OWNER.encode()).hexdigest()
    monkeypatch.setattr(api.app.state, "owner_key_hash", digest, raising=False)
    listed = TestClient(api.app).get("/jobs", headers={"X-Pi-Owner-Key": OWNER})
    assert listed.status_code == 200
    [view] = listed.json()["results"]
    assert view["id"] == rhythm.MORNING_CHECK_IN
    assert view["definition"]["name"] == "Morning check-in"
    assert view["definition"]["createdBy"] == "conker" and view["definition"]["state"] == "enabled"


class IdleWorker:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def close(self):
        pass


def start(monkeypatch, tmp_path, *, scheduler):
    for name in list(os.environ):
        if name.startswith("PI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PI_ADMIN_KEY", "synthetic-admin-key-only")
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "startup.db"))
    monkeypatch.setenv("PI_TOOLGATE_KEY", "synthetic-toolgate-key")
    if scheduler:
        monkeypatch.setenv("PI_SCHEDULER_ENABLED", "true")
    monkeypatch.setattr(startup, "JobWorker", IdleWorker)

    def no_network(*args, **kwargs):
        pytest.fail("Startup must not contact external services")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)

    async def run():
        async with startup.lifespan(api.app):
            pass

    asyncio.run(run())
    with closing(Store(tmp_path / "startup.db")) as store:
        return ai_jobs(store)


@pytest.mark.parametrize(("scheduler", "expected"), [(True, 1), (False, 0)])
def test_pi_seeds_it_at_start_only_where_jobs_run(monkeypatch, tmp_path, scheduler, expected):
    assert len(start(monkeypatch, tmp_path, scheduler=scheduler)) == expected
    # A second start makes nothing new.
    assert len(start(monkeypatch, tmp_path, scheduler=scheduler)) == expected
