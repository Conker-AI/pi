"""Shared builders for the team-execution tests: a recorder adapter, a configured store, run start and execute."""

from contextlib import closing

import pytest

from pi.agents import profiles as agents
from pi.agents import team_execution as teams
from pi.agents import team_models
from pi.agents import teams as collaboration
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.turns.loop import Loop


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
    definition = team_models.Team.model_validate(
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
        team_models.Start(
            request_id="team_run_request_001",
            expected_revision=1,
            source_session_id=sid,
            text=text,
            budgetMode="observed-stop",
            acknowledgeCurrentCallMayOvershoot=True,
        ),
    )


def execute(store, provider, run, second=False, **changes):
    body = team_models.Step(
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
