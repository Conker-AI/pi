"""Execute actual team turns with local recorder adapters, never provider networks."""

import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import (
    agents,
    collaboration,
    forgetting,
    model_roles,
    session_settings,
    tasks,
    team_execution_api,
)
from pi import team_execution as teams
from pi.loop import Loop, TurnFailed
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store
from pi.toolgate import Tool, ToolResult


class Recorder:
    name = "recorder"

    def __init__(self, usage=True, cost=0, tokens=2, callback=None):
        self.calls = []
        self.usage, self.cost, self.tokens, self.callback = usage, cost, tokens, callback

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((model, messages))
        if self.callback:
            self.callback()
        return Completion(
            text="role result",
            provider=self.name,
            model=model,
            input_tokens=self.tokens if self.usage else None,
            output_tokens=self.tokens if self.usage else None,
            cost_usd=self.cost if self.usage else None,
            citations=[{"id": "cite", "label": "supplied reference"}],
        )


def configure(store):
    disabled = dict(
        enabled=False,
        eligibleModelIds=[],
        modelId=None,
        timeoutMs=1000,
        failure="stop",
        fallbackModelId=None,
    )
    enabled = {**disabled, "enabled": True, "eligibleModelIds": ["answer"], "modelId": "answer"}
    configuration = model_roles.Configuration.model_validate(
        {
            "providers": [{"id": "recorder", "name": "Recorder", "enabled": True}],
            "models": [
                {
                    "id": "answer",
                    "providerId": "recorder",
                    "name": "Answer",
                    "route": "frozen-route",
                    "enabled": True,
                }
            ],
            "defaultModelId": "answer",
            "roleSettings": {
                "answerMode": "manual",
                "roles": {
                    **{role: dict(disabled) for role in model_roles.ROLES},
                    "answer": enabled,
                },
            },
        }
    )
    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=configuration))


def setup(store, source_ids=None, tokens=100, cost=100, memory_scope="none"):
    configure(store)
    agent = agents.create(
        store,
        agents.AgentInput(
            name="Reader",
            role="Review",
            instructions="Frozen agent instructions",
            modelId="answer",
            toolIds=["read", "write"],
            memory=agents.MemorySelection(scope=memory_scope, memoryIds=[]),
        ),
    )
    roles = [
        {
            "id": identity,
            "name": identity.title(),
            "agentId": agent["id"],
            "instructions": "Role " + identity,
            "toolIds": ["read"] if identity == "first" else [],
            "memory": {"scope": memory_scope, "memoryIds": []},
            "context": {
                "mode": "selected" if source_ids else "task_only",
                "sourceIds": source_ids or [],
            },
            "budget": {"maxTurns": 1, "maxTokens": tokens, "maxCostCents": cost},
        }
        for identity in ("first", "second")
    ]
    definition = collaboration.Team.model_validate(
        {
            "name": "Review",
            "objective": "Assess",
            "roles": roles,
            "handoffs": [
                {
                    "id": "review",
                    "fromRoleId": "first",
                    "toRoleId": "second",
                    "condition": "Owner reviewed the first result",
                    "payload": "result_only",
                    "maxTransfers": 1,
                }
            ],
            "budget": {
                "maxTurns": 2,
                "maxTokens": tokens * 2,
                "maxCostCents": cost * 2,
                "maxHandoffs": 1,
            },
        }
    )
    record = collaboration.save(store, "team", definition)
    return record, agent


def start(store, record, sid, text="Owner task"):
    return teams.start(
        store,
        record["id"],
        teams.Start(
            request_id="team_run_request_001",
            expected_revision=1,
            source_session_id=sid,
            text=text,
            budgetMode="observed-stop",
            acknowledgeCurrentCallMayOvershoot=True,
        ),
    )


def execute(store, provider, run, second=False, **changes):
    body = teams.Step(
        **{
            "request_id": "second_step_request_001" if second else "first_step_request_001",
            "expected_revision": run["revision"],
            "roleId": "second" if second else "first",
            "handoffId": "review" if second else None,
            "conditionReviewed": second,
            **changes,
        }
    )
    return teams.execute(store, Loop(store, Router(local_provider=provider)), run["id"], body)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "team.db")) as value:
        yield value


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
        teams.Start(
            request_id="team_run_request_001",
            expected_revision=1,
            source_session_id="session",
            text="task",
            budgetMode="observed-stop",
            acknowledgeCurrentCallMayOvershoot=False,
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
    first_body = teams.Step(
        request_id="first_tools_request_001", expected_revision=run["revision"], roleId="first"
    )
    first = teams.execute(store, runtime, run["id"], first_body)
    assert gate.invoked == ["read"] and len(first["modelCalls"]) == 2
    assert all("- write" not in m.content for m in provider.calls[0][1])
    second_body = teams.Step(
        request_id="second_tools_request_001",
        expected_revision=first["revision"],
        roleId="second",
        handoffId="review",
        conditionReviewed=True,
    )
    second = teams.execute(store, runtime, run["id"], second_body)
    assert gate.invoked == ["read"] and len(second["modelCalls"]) == 3
    assert all("TOOL OBSERVATION MUST NOT TRANSFER" not in m.content for m in provider.calls[2][1])
    teams.execute(store, runtime, run["id"], first_body)
    assert len(gate.invoked) == 1 and len(provider.calls) == 3
    finished = teams.finish(
        store, run["id"], teams.Finish(expected_revision=second["revision"], state="completed")
    )
    assert teams.reconcile(store, run["id"])["state"] == "completed"
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
    body = teams.Start(
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
    body = teams.Step(
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
    definition = collaboration.Team.model_validate(record["definition"])
    for role in definition.roles:
        role.budget.maxTurns = 2
    definition.budget.maxTurns = 4
    definition.budget.maxHandoffs = 2
    definition.handoffs[0].payload = "result_and_citations"
    definition.handoffs.append(
        collaboration.Handoff(
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
        teams.Start(
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
