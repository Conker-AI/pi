"""Gateway writes for agents, projects, teams and cron jobs."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from gateway.mutation import MutationError, apply_resource
from pi.api.browser_contract import owner_allowed
from tests.test_gateway_mutation import agent_configuration, config, team_definition


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {"operation": "create", "configuration": agent_configuration()},
            "/agents",
            agent_configuration(),
        ),
        (
            {
                "operation": "update",
                "id": "companion",
                "expected_revision": 4,
                "configuration": agent_configuration(),
            },
            "/agents/companion/update",
            {"expected_revision": 4, "configuration": agent_configuration()},
        ),
        (
            {
                "operation": "archive",
                "id": "agent_" + "a" * 32,
                "expected_revision": 5,
                "archived": True,
            },
            "/agents/agent_" + "a" * 32 + "/archive",
            {"expected_revision": 5, "archived": True},
        ),
    ],
)
def test_agent_apply_resolves_strict_operation_to_fixed_owner_route(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1, "revision": 6})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "agents", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 6
    assert seen[0].url.path == path
    assert owner_allowed("POST", path)
    assert json.loads(seen[0].content) == forwarded


def test_agent_apply_rejects_unknown_operation_companion_archive_and_extra_fields():
    invalid = [
        {"operation": "execute", "configuration": agent_configuration()},
        {
            "operation": "archive",
            "id": "companion",
            "expected_revision": 1,
            "archived": True,
        },
        {
            "operation": "create",
            "configuration": agent_configuration(),
            "authority": "admin",
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(config(), "agents", io.BytesIO(json.dumps(payload).encode()), client)


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {
                "operation": "create",
                "fields": {"name": "Launch", "description": "", "instructions": ""},
            },
            "/projects",
            {"name": "Launch", "description": "", "instructions": ""},
        ),
        (
            {
                "operation": "update",
                "id": "project_" + "b" * 32,
                "expected_revision": 2,
                "fields": {
                    "name": "Launch",
                    "description": "Ship it",
                    "instructions": "Carefully",
                },
            },
            "/projects/project_" + "b" * 32 + "/update",
            {
                "expected_revision": 2,
                "fields": {
                    "name": "Launch",
                    "description": "Ship it",
                    "instructions": "Carefully",
                },
            },
        ),
        (
            {
                "operation": "archive",
                "id": "project_" + "b" * 32,
                "expected_revision": 3,
                "archived": False,
            },
            "/projects/project_" + "b" * 32 + "/archive",
            {"expected_revision": 3, "archived": False},
        ),
        (
            {
                "operation": "link",
                "id": "project_" + "b" * 32,
                "expected_revision": 4,
                "reference": {"kind": "conversation", "sessionId": "ses_" + "c" * 16},
            },
            "/projects/project_" + "b" * 32 + "/link",
            {
                "expected_revision": 4,
                "reference": {"kind": "conversation", "sessionId": "ses_" + "c" * 16},
            },
        ),
        (
            {
                "operation": "unlink",
                "id": "project_" + "b" * 32,
                "expected_revision": 5,
                "reference": {"kind": "task", "taskId": "tsk_" + "d" * 32},
            },
            "/projects/project_" + "b" * 32 + "/unlink",
            {
                "expected_revision": 5,
                "reference": {"kind": "task", "taskId": "tsk_" + "d" * 32},
            },
        ),
    ],
)
def test_project_apply_resolves_typed_operations(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1, "revision": 6})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "projects", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 6
    assert seen[0].url.path == path
    assert owner_allowed("POST", path)
    assert json.loads(seen[0].content) == forwarded


def test_project_apply_rejects_removal_and_invalid_reference_before_network():
    invalid = [
        {
            "operation": "remove",
            "id": "project_" + "b" * 32,
            "expected_revision": 1,
        },
        {
            "operation": "link",
            "id": "project_" + "b" * 32,
            "expected_revision": 1,
            "reference": {"kind": "file", "sessionId": "bad", "fileId": "bad"},
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "projects", io.BytesIO(json.dumps(payload).encode()), client
                )


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {"operation": "create", "definition": team_definition()},
            "/collaboration/teams",
            team_definition(),
        ),
        (
            {
                "operation": "update",
                "id": "team_" + "e" * 32,
                "expected_revision": 2,
                "definition": team_definition(),
            },
            "/collaboration/teams/team_" + "e" * 32 + "/update",
            {"expected_revision": 2, "definition": team_definition()},
        ),
        (
            {
                "operation": "archive",
                "id": "team_" + "e" * 32,
                "expected_revision": 3,
            },
            "/collaboration/teams/team_" + "e" * 32 + "/archive",
            {"expected_revision": 3, "archived": True},
        ),
        (
            {
                "operation": "restore",
                "id": "team_" + "e" * 32,
                "expected_revision": 4,
            },
            "/collaboration/teams/team_" + "e" * 32 + "/restore",
            {"expected_revision": 4},
        ),
    ],
)
def test_team_apply_changes_configuration_without_exposing_execution(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1, "revision": 5})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(config(), "teams", io.BytesIO(json.dumps(payload).encode()), client)

    assert result["revision"] == 5
    assert seen[0].url.path == path
    assert owner_allowed("POST", path)
    assert json.loads(seen[0].content) == forwarded
    assert "/prepare" not in path and "/team-runs" not in path


def test_team_apply_rejects_prepare_execute_and_invalid_budget_before_network():
    invalid = [
        {
            "operation": "prepare",
            "id": "team_" + "e" * 32,
            "expected_revision": 1,
        },
        {"operation": "execute", "definition": team_definition()},
        {
            "operation": "create",
            "definition": team_definition()
            | {
                "budget": {
                    "maxTurns": 1,
                    "maxTokens": 1,
                    "maxCostCents": 0,
                    "maxHandoffs": 0,
                }
            },
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(config(), "teams", io.BytesIO(json.dumps(payload).encode()), client)


@pytest.mark.parametrize(
    ("payload", "path", "forwarded", "status"),
    [
        (
            {
                "operation": "state",
                "id": "job_" + "f" * 32,
                "expected_revision": 7,
                "enabled": False,
            },
            "/jobs/job_" + "f" * 32 + "/state",
            {"expected_revision": 7, "enabled": False},
            200,
        ),
        (
            {
                "operation": "run",
                "id": "job_" + "f" * 32,
                "request_id": "cli:1234567890abcdef",
            },
            "/jobs/job_" + "f" * 32 + "/run",
            {"request_id": "cli:1234567890abcdef"},
            202,
        ),
    ],
)
def test_job_apply_matches_connected_state_and_run_operations(payload, path, forwarded, status):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(status, json={"schemaVersion": 1, "status": "ready"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(config(), "jobs", io.BytesIO(json.dumps(payload).encode()), client)

    assert result["status"] == "ready"
    assert seen[0].url.path == path
    assert owner_allowed("POST", path)
    assert json.loads(seen[0].content) == forwarded


def test_job_apply_rejects_raw_authoring_and_unstable_run_identity_before_network():
    invalid = [
        {"operation": "create", "definition": {}},
        {
            "operation": "update",
            "id": "job_" + "f" * 32,
            "expected_revision": 1,
            "definition": {},
        },
        {"operation": "run", "id": "job_" + "f" * 32, "request_id": "short"},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(config(), "jobs", io.BytesIO(json.dumps(payload).encode()), client)
