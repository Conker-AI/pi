"""Team execution: per-role tools and metering, late results, the owner router, receipts and citations."""

import json
from contextlib import closing

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi.actions.toolgate import Tool, ToolResult
from pi.agents import profiles as agents
from pi.agents import team_execution as teams
from pi.agents import team_models
from pi.agents import teams as collaboration
from pi.api.routes import team_execution as team_execution_api
from pi.conversation.store import Store
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.turns.failures import TurnFailed
from pi.turns.loop import Loop
from tests.team_execution_helpers import (
    Recorder,
    execute,
    setup,
    start,
    store,  # noqa: F401 -- shared fixture
)


def test_real_tools_remain_per_role_and_every_reply_is_metered(store):
    class Tools:
        def __init__(self):
            self.invoked = []

        def tools(self):
            return [Tool(i, i, "Tool " + i, []) for i in ("read", "write")]

        def invoke(self, identity, args, **kwargs):
            self.invoked.append(identity)
            return ToolResult(True, "TOOL OBSERVATION MUST NOT TRANSFER", identity)

    class ToolRecorder(Recorder):
        def complete_bounded(self, messages, *, model, timeout):
            reply = super().complete_bounded(messages, model=model, timeout=timeout)
            if len(self.calls) == 1:
                return Completion(
                    text='{"tool":"read","args":{}}',
                    model=model,
                    provider=self.name,
                    input_tokens=2,
                    output_tokens=2,
                    cost_usd=0,
                )
            if len(self.calls) == 3:
                # Other role cannot act even when the model explicitly requests a tool.
                return Completion(
                    text='{"tool":"read","args":{}}',
                    model=model,
                    provider=self.name,
                    input_tokens=2,
                    output_tokens=2,
                    cost_usd=0,
                )
            return reply

    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    provider, gate = ToolRecorder(), Tools()
    runtime = Loop(store, Router(local_provider=provider), toolgate=gate)
    first_body = team_models.Step(
        request_id="first_tools_request_001", expected_revision=run["revision"], roleId="first"
    )
    first = teams.execute(store, runtime, run["id"], first_body)
    assert gate.invoked == ["read"] and len(first["modelCalls"]) == 2
    assert all("- write" not in m.content for m in provider.calls[0][1])
    second_body = team_models.Step(
        request_id="second_tools_request_001",
        expected_revision=first["revision"],
        roleId="second",
        handoffId="review",
        conditionReviewed=True,
    )
    with pytest.raises(TurnFailed, match="model requested an unavailable tool"):
        teams.execute(store, runtime, run["id"], second_body)
    second = teams.get(store, run["id"])
    assert second["state"] == "blocked" and second["steps"][-1]["state"] == "blocked"
    refused = store.turns(second["steps"][-1]["session_id"])
    assert len(refused) == 1 and refused[0]["status"] == "failed"
    assert refused[0]["acted"] == 0
    assert (
        refused[0]["detail"]
        == "The model requested an unavailable tool. This request was not executed."
    )
    assert gate.invoked == ["read"] and len(second["modelCalls"]) == 3
    assert all("TOOL OBSERVATION MUST NOT TRANSFER" not in m.content for m in provider.calls[2][1])
    teams.execute(store, runtime, run["id"], first_body)
    assert len(gate.invoked) == 1 and len(provider.calls) == 3
    with pytest.raises(agents.AgentError, match="Blocked work cannot be marked completed"):
        teams.finish(
            store,
            run["id"],
            team_models.Finish(expected_revision=second["revision"], state="completed"),
        )
    finished = teams.finish(
        store,
        run["id"],
        team_models.Finish(expected_revision=second["revision"], state="cancelled"),
    )
    assert teams.reconcile(store, run["id"])["state"] == "cancelled"
    with pytest.raises(agents.AgentError, match="Resolve"):
        execute(store, provider, finished, request_id="after_finish_request_001")


def test_late_provider_result_after_recovery_is_not_applied(store):
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    provider = Recorder(callback=lambda: teams.recover_interrupted(store))
    with pytest.raises(TurnFailed):
        execute(store, provider, run)
    result = teams.get(store, run["id"])
    assert result["state"] == "interrupted"
    assert result["modelCalls"][0]["state"] == "unknown"
    assert len(provider.calls) == 1


def test_strict_turn_allocation_and_completion(store):
    sid = store.create_session()
    record, _ = setup(store)
    provider = Recorder()
    first = execute(store, provider, start(store, record, sid))
    second = execute(store, provider, first, second=True)
    with pytest.raises(agents.AgentError, match="turn allocation"):
        execute(store, provider, second, request_id="third_step_request_001")
    assert len(provider.calls) == 2


def test_owner_router_executes_only_explicit_step(store):
    sid = store.create_session()
    record, _ = setup(store)
    app, provider = FastAPI(), Recorder()

    def authorize(x_owner_key: str | None = Header(None)):
        if x_owner_key != "owner":
            raise HTTPException(401)

    runtime = Loop(store, Router(local_provider=provider))
    app.include_router(team_execution_api.create_router(lambda: store, lambda: runtime, authorize))
    client, headers = TestClient(app), {"X-Owner-Key": "owner"}
    url = "/team-runs/from-team/" + record["id"]
    body = team_models.Start(
        request_id="team_run_request_001",
        expected_revision=1,
        source_session_id=sid,
        text="Task",
        budgetMode="observed-stop",
        acknowledgeCurrentCallMayOvershoot=True,
    ).model_dump()
    assert client.post(url, json=body).status_code == 401
    run = client.post(url, json=body, headers=headers).json()
    assert client.get("/team-runs/" + run["id"], headers=headers).status_code == 200
    assert not provider.calls
    url = "/team-runs/" + run["id"] + "/steps"
    body = team_models.Step(
        request_id="first_step_request_001", expected_revision=run["revision"], roleId="first"
    ).model_dump()
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=headers).status_code == 200
    assert client.post(url, json=body, headers=headers).status_code == 200
    assert len(provider.calls) == 1


def test_completed_receipt_survives_reopen_without_dispatch(tmp_path):
    path = tmp_path / "reopen.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        record, _ = setup(store)
        run = start(store, record, sid)
        completed = execute(store, Recorder(), run)
    with closing(Store(path)) as store:
        provider = Recorder()
        assert teams.get(store, run["id"])["steps"] == completed["steps"]
        assert execute(store, provider, run)["steps"] == completed["steps"]
        assert not provider.calls


def test_citations_are_explicit_and_handoff_counts_are_strict(store):
    sid = store.create_session()
    record, _ = setup(store)
    definition = team_models.Team.model_validate(record["definition"])
    for role in definition.roles:
        role.budget.maxTurns = 2
    definition.budget.maxTurns = 4
    definition.budget.maxHandoffs = 2
    definition.handoffs[0].payload = "result_and_citations"
    definition.handoffs.append(
        team_models.Handoff(
            id="back",
            fromRoleId="second",
            toRoleId="first",
            condition="Owner requests another pass",
            payload="result_only",
            maxTransfers=1,
        )
    )
    record = collaboration.save(store, "team", definition, record["id"], 1)
    run = teams.start(
        store,
        record["id"],
        team_models.Start(
            request_id="team_run_request_001",
            expected_revision=2,
            source_session_id=sid,
            text="Task",
            budgetMode="observed-stop",
            acknowledgeCurrentCallMayOvershoot=True,
        ),
    )
    provider = Recorder()
    first = execute(store, provider, run)
    second = execute(store, provider, first, second=True)
    payload = json.loads(provider.calls[1][1][-1].content)
    assert payload["handoff"]["citations"][0]["id"] == "cite"
    third = execute(
        store,
        provider,
        second,
        request_id="third_step_request_001",
        handoffId="back",
        conditionReviewed=True,
    )
    with pytest.raises(agents.AgentError, match="handoff allocation"):
        execute(store, provider, third, second=True, request_id="fourth_step_request_001")
    assert len(provider.calls) == 3
