"""Execute actual team turns with local recorder adapters, never provider networks."""

import json
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError

from pi.agents import profiles as agents
from pi.agents import team_execution as teams
from pi.agents import team_models
from pi.conversation import forgetting
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models.routing import Router
from pi.schedule import tasks
from pi.turns.failures import TurnFailed
from pi.turns.loop import Loop
from tests.team_execution_helpers import (
    Recorder,
    execute,
    setup,
    start,
    store,  # noqa: F401 -- shared fixture
)


def test_real_roles_frozen_configuration_selected_context_and_handoff(store):
    sid = store.create_session()
    chosen = store.append_message(sid, "user", "Selected evidence")
    store.append_message(sid, "user", "Unselected secret")
    record, agent = setup(store, [chosen["id"]])
    run = start(store, record, sid)
    edited = agents.AgentInput.model_validate(
        {**agent["configuration"], "instructions": "Later change"}
    )
    agents.update(store, agent["id"], agents.UpdateAgent(expected_revision=1, configuration=edited))
    models = model_roles.Configuration.model_validate(model_roles.load(store)["configuration"])
    models.models[0].route = "later-route"
    model_roles.save(store, model_roles.Update(expected_revision=1, configuration=models))
    provider = Recorder()
    first = execute(store, provider, run)
    assert first["state"] == "ready"
    assert len(first["steps"]) == 1 and len(first["modelCalls"]) == 1
    assert first["modelCalls"][0]["actual_model"] == "frozen-route"
    messages = [m.content for m in provider.calls[0][1]]
    assert any("Frozen agent instructions" in m for m in messages)
    assert all("Later change" not in m and "Unselected secret" not in m for m in messages)
    assert "Selected evidence" in messages[-1]
    task = tasks.get(store, first["steps"][0]["task_id"])
    assert len(task["run_ids"]) == 1
    second = execute(store, provider, first, second=True)
    assert len(provider.calls) == 2
    handed = json.loads(provider.calls[1][1][-1].content)["handoff"]
    assert handed["result"] == "role result" and "citations" not in handed
    assert second["steps"][1]["handoff_id"] == "review"
    with pytest.raises(agents.AgentError, match="does not belong"):
        session_settings.execution(store, second["steps"][1]["session_id"], task["run_ids"][0])
    with store._connect() as db:
        snap = teams.session_snapshot(db, first["steps"][0]["session_id"])
        assert snap["configuration"]["toolIds"] == ["read"] and snap["kind"] == "team-role"
        assert snap["project"] is None and snap["privacy"]["memoryDisabled"]
        assert snap["projectContext"] == []
        assert (
            db.execute("SELECT COUNT(*) FROM memory_outbox WHERE operation='ingest'").fetchone()[0]
            == 2
        )


def test_replay_does_not_repeat_model_or_change_request(store):
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    provider = Recorder()
    first = execute(store, provider, run)
    assert execute(store, provider, run)["steps"] == first["steps"]
    assert len(provider.calls) == 1
    with pytest.raises(agents.AgentError, match="already used"):
        execute(store, provider, run, roleId="second")
    with pytest.raises(agents.AgentError, match="Review"):
        execute(store, provider, first, second=True, conditionReviewed=False)
    with pytest.raises(agents.AgentError, match="Reload"):
        execute(store, provider, run, request_id="stale_step_request_001")


@pytest.mark.parametrize("usage,cost,tokens", [(False, 0, 2), (True, 2, 2), (True, 0, 60)])
def test_unknown_or_exceeded_observed_usage_stops_without_second_call(store, usage, cost, tokens):
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    provider = Recorder(usage, cost, tokens)
    with pytest.raises(TurnFailed):
        execute(store, provider, run)
    current = teams.get(store, run["id"])
    assert current["state"] == "blocked" and len(current["modelCalls"]) == 1
    assert len(provider.calls) == 1
    assert execute(store, provider, run)["state"] == "blocked"
    with pytest.raises(agents.AgentError, match="Resolve"):
        execute(store, provider, current, second=True)


@pytest.mark.parametrize("priced", [False, True])
def test_a_provider_that_reports_no_price_counts_tokens_only(store, monkeypatch, priced):
    if priced:
        monkeypatch.setattr(teams, "PRICED", frozenset({Recorder.name}))
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    provider = Recorder(cost=None)
    if not priced:
        # Local models, plans and direct keys report no price: the answer is kept.
        assert execute(store, provider, run)["steps"][0]["state"] == "complete"
        call = teams.get(store, run["id"])["modelCalls"][0]
        assert call["state"] == "complete"
    else:
        # OpenRouter always reports one, so a missing price is unknown usage.
        with pytest.raises(TurnFailed):
            execute(store, provider, run)
        assert teams.get(store, run["id"])["state"] == "blocked"


def test_zero_cost_blocks_before_call(store):
    sid = store.create_session()
    record, _ = setup(store, cost=0)
    run = start(store, record, sid)
    provider = Recorder()
    with pytest.raises(agents.AgentError, match="cost threshold"):
        execute(store, provider, run)
    assert provider.calls == []


def test_crash_recovery_never_reexecutes(store):
    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    provider = Recorder(callback=crash)
    with pytest.raises(Crash):
        execute(store, provider, run)
    store.mark_interrupted_turns()
    assert teams.recover_interrupted(store) == 1
    assert execute(store, provider, run)["state"] == "interrupted"
    assert teams.reconcile(store, run["id"])["state"] == "interrupted"
    assert len(provider.calls) == 1


def test_private_and_foreign_selected_sources_fail_closed(store):
    sid = store.create_session()
    other = store.create_session()
    message = store.append_message(other, "user", "foreign")
    record, _ = setup(store, [message["id"]])
    with pytest.raises(agents.AgentError, match="privacy"):
        start(store, record, sid)


def test_step_session_rejects_unrelated_submission_and_policy_edit(store):
    sid = store.create_session()
    record, _ = setup(store)
    run = execute(store, Recorder(), start(store, record, sid))
    step = run["steps"][0]
    with pytest.raises(sqlite3.IntegrityError, match="reserved step"):
        Loop(store, Router(local_provider=Recorder())).run_turn(
            step["session_id"], "Inject", request_id="unrelated_request_001"
        )
    with store._connect() as db, pytest.raises(sqlite3.IntegrityError, match="fixed"):
        db.execute(
            "UPDATE context_policies SET revision=revision+1 WHERE session_id=?",
            (step["session_id"],),
        )


def test_forgetting_covers_source_task_and_derived_role_text(tmp_path):
    path = tmp_path / "forget.db"
    secret = "team-secret-source-817235"
    with closing(Store(path)) as store:
        sid = store.create_session()
        chosen = store.append_message(sid, "user", secret)
        record, _ = setup(store, [chosen["id"]])
        run = execute(store, Recorder(), start(store, record, sid, text="private-task-621849"))
        run = execute(store, Recorder(), run, second=True)
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    assert secret.encode() not in path.read_bytes()
    assert b"private-task-621849" not in path.read_bytes()
    with closing(Store(path)) as store, pytest.raises(tasks.TaskError):
        teams.get(store, run["id"])


def test_explicit_budget_acknowledgement_required():
    with pytest.raises(ValidationError):
        team_models.Start(
            request_id="team_run_request_001",
            expected_revision=1,
            source_session_id="session",
            text="task",
            budgetMode="observed-stop",
            acknowledgeCurrentCallMayOvershoot=False,
        )
