"""A role step started with `background: true` answers at once and finishes on its own thread."""

import logging
import threading
import time
from contextlib import closing

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from test_spending_cap import set_cap
from test_team_execution import Recorder, setup, start

from pi.agents import team_background, team_execution, team_models
from pi.api.routes import team_execution as team_execution_api
from pi.conversation.store import Store
from pi.core import errors
from pi.models.routing import Router
from pi.turns.loop import Loop

HEADERS = {"X-Owner-Key": "owner"}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "team-background.db")) as value:
        yield value


class Gate:
    """A provider callback that holds the role's model call until the test lets it go."""

    def __init__(self, fail=None):
        self.entered, self.go, self.fail = threading.Event(), threading.Event(), fail

    def __call__(self):
        self.entered.set()
        assert self.go.wait(20)
        if self.fail:
            raise RuntimeError(self.fail)


def client_for(store, provider):
    app = FastAPI()
    errors.install(app)

    def authorize(x_owner_key: str | None = Header(None)):
        if x_owner_key != "owner":
            raise HTTPException(401)

    runtime = Loop(store, Router(local_provider=provider))
    app.include_router(team_execution_api.create_router(lambda: store, lambda: runtime, authorize))
    return TestClient(app)


def step_body(run, request_id="first_step_request_001", **changes):
    return team_models.Step(
        request_id=request_id,
        expected_revision=run["revision"],
        roleId="first",
        background=True,
        **changes,
    ).model_dump()


def post_step(client, run, **changes):
    return client.post(
        f"/team-runs/{run['id']}/steps", json=step_body(run, **changes), headers=HEADERS
    )


def wait_for(check, what):
    deadline = time.time() + 20
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what}.")


def state(store, run):
    return team_execution.get(store, run["id"])["state"]


def slots_free():
    return team_background._slots._value == team_background.MAX_BACKGROUND_STEPS


def test_the_step_answers_at_once_while_the_role_is_still_working(store):
    gate = Gate()
    provider = Recorder(callback=gate)
    record, _ = setup(store)
    run = start(store, record, store.create_session())
    client = client_for(store, provider)
    reply = post_step(client, run)
    assert reply.status_code == 202 and reply.json()["state"] == "running"
    assert gate.entered.wait(20)
    polled = client.get(f"/team-runs/{run['id']}", headers=HEADERS).json()
    assert polled["state"] == "running" and len(provider.calls) == 1
    gate.go.set()
    wait_for(lambda: state(store, run) == "ready", "the run to be ready")
    finished = client.get(f"/team-runs/{run['id']}", headers=HEADERS).json()
    assert [item["state"] for item in finished["steps"]] == ["complete"]
    wait_for(slots_free, "the slot to be free")


def test_a_cap_that_is_off_refuses_before_any_thread_starts(store):
    provider = Recorder()
    record, _ = setup(store)
    run = start(store, record, store.create_session())
    set_cap(store, enabled=False)
    before = threading.active_count()
    reply = post_step(client_for(store, provider), run)
    assert reply.status_code == 409
    assert reply.json()["detail"]["code"] == "spending_cap_off"
    assert threading.active_count() == before and slots_free() and not provider.calls
    assert team_execution.get(store, run["id"])["steps"] == []


def test_a_replay_starts_no_second_turn(store):
    gate = Gate()
    provider = Recorder(callback=gate)
    record, _ = setup(store)
    run = start(store, record, store.create_session())
    client = client_for(store, provider)
    assert post_step(client, run).status_code == 202
    assert gate.entered.wait(20)
    again = post_step(client, run)
    assert again.status_code == 202 and again.json()["state"] == "running"
    inspected = client.post(
        f"/team-runs/{run['id']}/steps",
        json={**step_body(run), "background": False},
        headers=HEADERS,
    )
    assert inspected.status_code == 200 and inspected.json()["state"] == "running"
    gate.go.set()
    wait_for(lambda: state(store, run) == "ready", "the run to be ready")
    wait_for(slots_free, "the slot to be free")
    assert len(provider.calls) == 1


def test_a_failing_role_blocks_the_run_and_logs_no_words(store, caplog):
    gate = Gate(fail="private words from the turn")
    provider = Recorder(callback=gate)
    record, _ = setup(store)
    run = start(store, record, store.create_session())
    client = client_for(store, provider)
    with caplog.at_level(logging.ERROR):
        assert post_step(client, run).status_code == 202
        gate.go.set()
        wait_for(lambda: state(store, run) in ("blocked", "held"), "the run to stop")
        wait_for(slots_free, "the slot to be free")
    assert state(store, run) == "blocked"
    assert "private words" not in caplog.text


def test_a_restart_while_a_step_runs_leaves_the_run_interrupted(store):
    gate = Gate()
    record, _ = setup(store)
    run = start(store, record, store.create_session())
    client = client_for(store, Recorder(callback=gate))
    assert post_step(client, run).status_code == 202
    assert gate.entered.wait(20)
    team_execution.recover_interrupted(store)
    assert state(store, run) == "interrupted"
    gate.go.set()
    wait_for(slots_free, "the slot to be free")
    assert state(store, run) == "interrupted"


def test_too_many_steps_at_once_are_refused_not_queued(store, monkeypatch):
    monkeypatch.setattr(team_background, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(team_background, "MAX_BACKGROUND_STEPS", 1)
    gate = Gate()
    record, _ = setup(store)
    first = start(store, record, store.create_session())
    second = team_execution.start(
        store,
        record["id"],
        team_models.Start(
            request_id="team_run_request_002",
            expected_revision=1,
            source_session_id=store.create_session(),
            text="Another task",
            budgetMode="observed-stop",
            acknowledgeCurrentCallMayOvershoot=True,
        ),
    )
    client = client_for(store, Recorder(callback=gate))
    assert post_step(client, first).status_code == 202
    assert gate.entered.wait(20)
    refused = post_step(client, second, request_id="second_run_step_request")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "team_busy"
    assert team_execution.get(store, second["id"])["steps"] == []
    gate.go.set()
    wait_for(slots_free, "the slot to be free")
