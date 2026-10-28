from __future__ import annotations

import io
import json

import httpx
import pytest

from gateway.api import Config
from gateway.mutation import MAX_REQUEST_BYTES, MutationError, apply_resource, resources
from gateway.toolgate_contract import owner_request_allowed
from pi.api.browser_contract import owner_allowed, runtime_allowed


def config() -> Config:
    return Config(
        "https://conker.test",
        "/auth/auth.db",
        "http://pi.test",
        "runtime-" + "r" * 32,
        "http://toolgate.test",
        "owner-" + "o" * 32,
        pi_owner_key="control-" + "c" * 32,
        toolgate_execution_key="tgx_" + "e" * 32,
    )


def agent_configuration():
    return {
        "name": "Researcher",
        "role": "Research",
        "instructions": "Research carefully.",
        "modelId": None,
        "toolIds": [],
        "toolAccess": {},
        "memory": {"scope": "conversation", "memoryIds": []},
    }


def team_definition():
    return {
        "name": "Research team",
        "objective": "Prepare an evidence-backed answer.",
        "roles": [
            {
                "id": "researcher",
                "name": "Researcher",
                "agentId": "companion",
                "instructions": "Collect evidence.",
                "toolIds": [],
                "memory": {"scope": "conversation", "memoryIds": []},
                "context": {"mode": "task_only", "sourceIds": []},
                "budget": {"maxTurns": 2, "maxTokens": 1000, "maxCostCents": 0},
            }
        ],
        "handoffs": [],
        "budget": {
            "maxTurns": 2,
            "maxTokens": 1000,
            "maxCostCents": 0,
            "maxHandoffs": 0,
        },
    }


def editor_document(identity="example"):
    return {
        "id": identity,
        "name": "Example",
        "description": "",
        "kind": "workflow",
        "nodes": [
            {
                "id": "input",
                "type": "input",
                "label": "Input",
                "position": {"x": 0, "y": 0},
                "config": {},
            },
            {
                "id": "result",
                "type": "return",
                "label": "Return",
                "position": {"x": 200, "y": 0},
                "config": {"value": "$last"},
            },
        ],
        "edges": [{"id": "next", "source": "input", "target": "result"}],
        "inputs": [],
        "outputs": [],
        "credentialRefs": [],
        "effect": "read",
        "agentVisible": True,
        "budgets": {"maxSteps": 80, "maxLoopItems": 20, "timeoutMs": 5000},
    }


def test_model_apply_uses_fixed_owner_route_and_exact_json():
    payload = {"expected_revision": 4, "configuration": {"models": []}}
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"revision": 5, "configuration": {"models": []}})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "models", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert resources() == (
        "agents",
        "approvals",
        "artifacts",
        "asks",
        "calls",
        "character",
        "files",
        "inventory",
        "jobs",
        "memory",
        "models",
        "projects",
        "proposals",
        "requests",
        "sessions",
        "tasks",
        "teams",
        "tools",
        "turns",
    )
    assert result["revision"] == 5
    request = seen[0]
    assert request.method == "POST"
    assert request.url == httpx.URL("http://pi.test/models/configuration")
    assert owner_allowed(request.method, request.url.path)
    assert json.loads(request.content) == payload
    assert request.headers["x-pi-owner-key"] == config().pi_owner_key
    assert "x-pi-gateway-key" not in request.headers


def test_approval_apply_uses_only_fixed_toolgate_owner_decision_route():
    payload = {
        "operation": "decide",
        "id": "req_1234567890abcdef",
        "status": "approved",
        "note": "Reviewed against the displayed effect.",
    }
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"id": payload["id"], "status": "approved"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "approvals", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["status"] == "approved"
    request = seen[0]
    assert request.url == httpx.URL(
        "http://toolgate.test/v2/owner/requests/req_1234567890abcdef/decision"
    )
    assert owner_request_allowed(request.method, request.url.path)
    assert json.loads(request.content) == {
        "status": "approved",
        "note": "Reviewed against the displayed effect.",
    }
    assert request.headers["x-toolgate-owner-key"] == config().owner_key
    assert "x-toolgate-execution-key" not in request.headers
    assert "x-pi-owner-key" not in request.headers
    assert "x-pi-gateway-key" not in request.headers


def test_approval_apply_rejects_unbounded_shapes_and_missing_owner_key_before_network():
    invalid = [
        {"operation": "execute", "id": "req_1", "status": "approved"},
        {"operation": "decide", "id": "../health", "status": "approved"},
        {"operation": "decide", "id": "req_1", "status": "pending"},
        {
            "operation": "decide",
            "id": "req_1",
            "status": "rejected",
            "note": "x" * 2001,
        },
        {
            "operation": "decide",
            "id": "req_1",
            "status": "dismissed",
            "url": "https://attacker.invalid",
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "approvals", io.BytesIO(json.dumps(payload).encode()), client
                )

        missing = Config(
            "https://conker.test",
            "/auth/auth.db",
            "http://pi.test",
            "runtime-" + "r" * 32,
        )
        with pytest.raises(MutationError, match="ToolGate owner mutation"):
            apply_resource(
                missing,
                "approvals",
                io.BytesIO(json.dumps(invalid[2] | {"status": "approved"}).encode()),
                client,
            )


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        (
            {"operation": "resume", "id": "turn_1234567890abcdef"},
            "/turns/turn_1234567890abcdef/resume",
        ),
        (
            {
                "operation": "cancel_submission",
                "request_id": "request_1234567890abcdef",
            },
            "/turn-submissions/request_1234567890abcdef/cancel",
        ),
    ],
)
def test_turn_apply_uses_only_runtime_recovery_routes(payload, path):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"status": "cancelled"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(config(), "turns", io.BytesIO(json.dumps(payload).encode()), client)

    assert result["status"] == "cancelled"
    request = seen[0]
    assert request.url == httpx.URL("http://pi.test" + path)
    assert runtime_allowed("POST", path)
    assert json.loads(request.content) == {}
    assert request.headers["x-pi-gateway-key"] == config().pi_key
    assert "x-pi-owner-key" not in request.headers
    assert "x-toolgate-owner-key" not in request.headers


def test_turn_apply_rejects_non_recovery_operations_and_invalid_identities():
    invalid = [
        {"operation": "submit", "id": "turn_123"},
        {"operation": "resume", "id": "../health"},
        {"operation": "resume", "id": "turn_123", "job_id": "hidden"},
        {"operation": "cancel_submission", "request_id": "too-short"},
        {
            "operation": "cancel_submission",
            "request_id": "request_1234567890abcdef",
            "reason": "arbitrary",
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(config(), "turns", io.BytesIO(json.dumps(payload).encode()), client)


@pytest.mark.parametrize(
    ("resource", "payload", "path", "forwarded"),
    [
        (
            "files",
            {
                "operation": "request",
                "request_id": "listing_1234567890abcdef",
                "root_id": "project",
                "path": "docs",
                "limit": 50,
            },
            "/system/files/listings",
            {
                "request_id": "listing_1234567890abcdef",
                "root_id": "project",
                "path": "docs",
                "limit": 50,
            },
        ),
        (
            "files",
            {"operation": "resume", "id": "listing_1234567890abcdef"},
            "/system/files/listings/listing_1234567890abcdef/resume",
            {},
        ),
        (
            "inventory",
            {
                "operation": "request",
                "request_id": "inventory_1234567890abcdef",
                "limit": 100,
            },
            "/system/inventory",
            {"request_id": "inventory_1234567890abcdef", "limit": 100},
        ),
        (
            "inventory",
            {"operation": "resume", "id": "inventory_1234567890abcdef"},
            "/system/inventory/inventory_1234567890abcdef/resume",
            {},
        ),
    ],
)
def test_read_only_system_apply_uses_fixed_owner_routes(resource, payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"state": "complete"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), resource, io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["state"] == "complete"
    request = seen[0]
    assert request.url == httpx.URL("http://pi.test" + path)
    assert owner_allowed("POST", path)
    assert json.loads(request.content) == forwarded
    assert request.headers["x-pi-owner-key"] == config().pi_owner_key
    assert "x-pi-gateway-key" not in request.headers


@pytest.mark.parametrize(
    ("resource", "payload"),
    [
        (
            "files",
            {
                "operation": "request",
                "request_id": "listing_1234567890abcdef",
                "root_id": "project",
                "path": "../secret",
            },
        ),
        ("files", {"operation": "read", "id": "listing_1234567890abcdef"}),
        ("files", {"operation": "resume", "id": "short"}),
        (
            "inventory",
            {
                "operation": "request",
                "request_id": "inventory_1234567890abcdef",
                "limit": 201,
            },
        ),
        ("inventory", {"operation": "act", "id": "inventory_1234567890abcdef"}),
        ("inventory", {"operation": "resume", "id": "../daemon________"}),
    ],
)
def test_read_only_system_apply_rejects_unsafe_shapes_before_network(resource, payload):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(MutationError, match="unsupported shape"),
    ):
        apply_resource(config(), resource, io.BytesIO(json.dumps(payload).encode()), client)


def test_apply_rejects_unknown_invalid_and_oversized_input_before_network():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        with pytest.raises(MutationError, match="Unknown apply resource"):
            apply_resource(config(), "https://attacker.invalid", io.BytesIO(b"{}"), client)
        with pytest.raises(MutationError, match="invalid JSON"):
            apply_resource(config(), "models", io.BytesIO(b"{"), client)
        with pytest.raises(MutationError, match="JSON object"):
            apply_resource(config(), "models", io.BytesIO(b"[]"), client)
        with pytest.raises(MutationError, match="exceeded 2 MiB"):
            apply_resource(config(), "models", io.BytesIO(b"x" * (MAX_REQUEST_BYTES + 1)), client)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(302, headers={"location": "https://attacker.invalid"}), "HTTP 302"),
        (httpx.Response(409, text="private conflict detail"), "HTTP 409"),
        (httpx.Response(200, text="not json"), "non-JSON"),
        (
            httpx.Response(200, content=b"[]", headers={"content-type": "application/json"}),
            "unsupported",
        ),
    ],
)
def test_apply_rejects_redirects_denials_and_invalid_responses(response, message):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as client,
        pytest.raises(MutationError, match=message) as failure,
    ):
        apply_resource(config(), "models", io.BytesIO(b"{}"), client)
    assert "private conflict detail" not in str(failure.value)
