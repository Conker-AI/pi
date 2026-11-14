"""GET /insights/tomorrow (R-U10): what starts on its own soon. Read-only; jobs the clock fires,
held and watched jobs, approvals that run out; a hidden chat's parked turn is not listed."""

from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_agent_jobs import NOON, Loop, ai_job, claimed_run
from test_jobs import definition

from pi.agents import tomorrow
from pi.api.browser_contract import owner_allowed
from pi.conversation import archive
from pi.conversation.store import Store
from pi.schedule import agent_jobs, jobs

HOUR = 3600
OWNER = "synthetic-owner"


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


@pytest.fixture
def client(store, monkeypatch):
    from pi.api import server as api

    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    return TestClient(api.app)


def forecast(store, hours=24):
    page = tomorrow.tomorrow(store, NOON, hours)
    tomorrow.Tomorrow.model_validate(page)
    return page


def move(store, job, at):
    with store._connect() as db:
        db.execute("UPDATE scheduled_jobs SET next_at=? WHERE id=?", (at, job["id"]))


def park(store, session, expires, tool="t_send"):
    turn = store.start_turn(session)
    with store._connect() as db:
        db.execute(
            "UPDATE turns SET status='awaiting_approval', approval_tool_id=?, "
            "approval_expires_at=? WHERE id=?",
            (tool, expires, turn),
        )
    return turn


def iso(seconds_from_noon, zone=True):
    moment = datetime.fromtimestamp(NOON + seconds_from_noon, UTC)
    return moment.isoformat() if zone else moment.replace(tzinfo=None).isoformat()


def test_clock_jobs_report_next_time_and_how_often_they_fire(store):
    hourly = jobs.create(store, definition(), now=NOON)  # every hour from noon
    ai = agent_jobs.create(store, ai_job(), now=NOON)  # every two hours
    far = jobs.create(store, definition(name="Weekly"), now=NOON)
    move(store, far, NOON + 3 * 86400)
    page = forecast(store)
    by_id = {item["id"]: item for item in page["jobs"]}
    assert set(by_id) == {hourly["id"], ai["id"]}
    assert (by_id[hourly["id"]]["nextAt"], by_id[hourly["id"]]["runs"]) == (NOON + HOUR, 23)
    assert (by_id[ai["id"]]["nextAt"], by_id[ai["id"]]["runs"]) == (NOON + 2 * HOUR, 11)
    assert by_id[ai["id"]]["name"] == "Watch my week" and by_id[ai["id"]]["held"] is False
    assert page["runsPlanned"] == 34 and page["jobs"][0]["id"] == hourly["id"]
    assert forecast(store, 3)["runsPlanned"] == 2 + 1  # +1h, +2h and the AI job's +2h


def test_paused_clockless_and_watched_jobs(store):
    off = jobs.create(store, definition(enabled=False), now=NOON)
    hook = agent_jobs.create(store, ai_job(name="Hook", clock=False), now=NOON)
    watch = agent_jobs.create(
        store, ai_job(name="Watcher", watch=dict(toolId="t_get", everyMinutes=15)), now=NOON
    )
    page = forecast(store)
    assert [item["id"] for item in page["jobs"]] == [watch["id"]]
    only = page["jobs"][0]
    assert (only["trigger"], only["nextAt"], only["runs"]) == ("watch", None, None)
    assert off["id"] not in str(page) and hook["id"] not in str(page)
    assert page["runsPlanned"] == 0


def test_a_job_whose_last_run_waits_for_his_answer_is_held(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    session = agent_jobs.session_for(store, job["id"])
    turn = store.start_turn(session)
    with store._connect() as db:
        db.execute("UPDATE turns SET status='awaiting_approval' WHERE id=?", (turn,))
    run = claimed_run(store, job)
    loop = Loop({"session_id": session, "turn_id": turn, "status": "awaiting_approval"})
    assert jobs.dispatch_claim(store, run, agent_jobs.AgentJobs(store, loop)) == "completed"
    assert next(i for i in forecast(store)["jobs"] if i["id"] == job["id"])["held"] is True
    with store._connect() as db:
        db.execute("UPDATE turns SET status='complete' WHERE id=?", (turn,))
    assert next(i for i in forecast(store)["jobs"] if i["id"] == job["id"])["held"] is False


def test_approvals_listed_only_when_they_run_out_inside_the_window(store):
    shown = store.create_session(title="Work")
    hidden = store.create_session(title="Private")
    park(store, shown, iso(2 * HOUR))
    park(store, shown, iso(30 * HOUR))  # after the window
    park(store, shown, iso(HOUR, zone=False))  # no zone: never guessed
    park(store, hidden, iso(HOUR))
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=True))
    approvals = forecast(store)["approvals"]
    assert approvals == [{"sessionId": shown, "toolId": "t_send", "expiresAt": NOON + 2 * HOUR}]
    assert len(forecast(store, 48)["approvals"]) == 2
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=False))
    assert len(forecast(store)["approvals"]) == 2


def test_an_empty_database_forecasts_nothing(store):
    page = forecast(store)
    assert (page["jobs"], page["approvals"], page["runsPlanned"]) == ([], [], 0)
    assert page["until"] == NOON + 24 * HOUR


def test_the_forecast_changes_nothing(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    park(store, store.create_session(title="Work"), iso(HOUR))
    with store._connect() as db:
        before = [db.execute(f"SELECT * FROM {t}").fetchall() for t in ("scheduled_jobs", "turns")]
    forecast(store)
    with store._connect() as db:
        after = [db.execute(f"SELECT * FROM {t}").fetchall() for t in ("scheduled_jobs", "turns")]
    assert [[tuple(r) for r in t] for t in before] == [[tuple(r) for r in t] for t in after]
    assert jobs.get_job(store, job["id"])["next_at"] == NOON + 2 * HOUR


def test_the_route_is_owner_only_validates_hours_and_names_its_model(client):
    assert client.get("/insights/tomorrow").status_code == 401
    owner = {"X-Pi-Owner-Key": OWNER}
    body = client.get("/insights/tomorrow?hours=48", headers=owner)
    assert body.status_code == 200 and body.json()["hours"] == 48
    tomorrow.Tomorrow.model_validate(body.json())
    for hours in (0, 169):
        assert client.get(f"/insights/tomorrow?hours={hours}", headers=owner).status_code == 422
    assert owner_allowed("GET", "/insights/tomorrow")
    assert not owner_allowed("POST", "/insights/tomorrow")
    path = client.get("/openapi.json").json()["paths"]["/insights/tomorrow"]["get"]
    assert path["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Tomorrow"
    }
