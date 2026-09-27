from __future__ import annotations

import io
import json

import httpx
import pytest

from gateway.api import Config
from gateway.mutation import MAX_REQUEST_BYTES, MutationError, apply_resource, resources
from gateway.toolgate_contract import owner_editor_allowed, owner_request_allowed
from pi.browser_contract import owner_allowed, runtime_allowed
from tests.test_characters import profile as character_profile


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
        "calls",
        "character",
        "files",
        "inventory",
        "jobs",
        "memory",
        "models",
        "projects",
        "proposals",
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
        result = apply_resource(
            config(), "turns", io.BytesIO(json.dumps(payload).encode()), client
        )

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
                apply_resource(
                    config(), "turns", io.BytesIO(json.dumps(payload).encode()), client
                )


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
def test_read_only_system_apply_uses_fixed_owner_routes(
    resource, payload, path, forwarded
):
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


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {
                "operation": "save",
                "expected_revision": 1,
                "profile": character_profile(),
            },
            "/characters/companion/save",
            {"expected_revision": 1, "profile": character_profile()},
        ),
        (
            {"operation": "import", "text": '{"format":"conker-character"}'},
            "/characters/companion/import",
            {"text": '{"format":"conker-character"}'},
        ),
        (
            {"operation": "restore", "expected_revision": 3, "revision": 1},
            "/characters/companion/restore",
            {"expected_revision": 3, "revision": 1},
        ),
    ],
)
def test_character_apply_uses_typed_companion_owner_routes(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"agentId": "companion", "revision": 2})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "character", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 2
    request = seen[0]
    assert request.url == httpx.URL("http://pi.test" + path)
    assert owner_allowed("POST", path)
    assert json.loads(request.content) == forwarded
    assert request.headers["x-pi-owner-key"] == config().pi_owner_key


def test_character_apply_rejects_non_companion_and_untyped_operations_before_network():
    invalid = [
        {"operation": "delete"},
        {"operation": "restore", "expected_revision": 1, "revision": 0},
        {
            "operation": "restore",
            "expected_revision": 1,
            "revision": 1,
            "agent_id": "agent_" + "a" * 32,
        },
        {"operation": "import", "text": "{}", "url": "https://attacker.invalid"},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "character", io.BytesIO(json.dumps(payload).encode()), client
                )


def test_character_apply_uses_the_character_response_envelope():
    declared = 3 * 1024 * 1024
    response = httpx.Response(
        200,
        content=b"{}",
        headers={"content-type": "application/json", "content-length": str(declared)},
    )
    payload = {"operation": "restore", "expected_revision": 2, "revision": 1}
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as client:
        assert apply_resource(
            config(), "character", io.BytesIO(json.dumps(payload).encode()), client
        ) == {}


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {
                "operation": "start",
                "request_id": "call_start_1234567890",
                "conversationId": "ses_source_123",
            },
            "/calls/browser",
            {
                "request_id": "call_start_1234567890",
                "conversationId": "ses_source_123",
                "privacy": {"memory": False, "harness": False},
            },
        ),
        (
            {
                "operation": "update",
                "id": "call_" + "a" * 32,
                "expected_revision": 1,
                "paused": True,
            },
            "/calls/browser/call_" + "a" * 32 + "/update",
            {"expected_revision": 1, "paused": True},
        ),
        (
            {
                "operation": "interrupt",
                "id": "call_" + "b" * 32,
                "expected_revision": 2,
            },
            "/calls/browser/call_" + "b" * 32 + "/interrupt",
            {"expected_revision": 2},
        ),
        (
            {
                "operation": "end",
                "id": "call_" + "c" * 32,
                "expected_revision": 3,
            },
            "/calls/browser/call_" + "c" * 32 + "/end",
            {"expected_revision": 3},
        ),
        (
            {
                "operation": "turn",
                "id": "call_" + "d" * 32,
                "request_id": "call_turn_123456789",
                "text": "Think this through.",
            },
            "/calls/browser/call_" + "d" * 32 + "/turns",
            {
                "request_id": "call_turn_123456789",
                "text": "Think this through.",
                "language": "en",
            },
        ),
    ],
)
def test_call_apply_uses_typed_only_owner_routes(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1, "audioIncluded": False})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "calls", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["audioIncluded"] is False
    request = seen[0]
    assert request.url == httpx.URL("http://pi.test" + path)
    assert owner_allowed("POST", path)
    assert json.loads(request.content) == forwarded
    assert request.headers["x-pi-owner-key"] == config().pi_owner_key


def test_call_apply_rejects_media_and_untyped_routes_before_network():
    call_id = "call_" + "a" * 32
    invalid = [
        {"operation": "audio", "id": call_id, "request_id": "request_123456789"},
        {"operation": "turn", "id": "../call", "request_id": "request_123456789"},
        {
            "operation": "turn",
            "id": call_id,
            "request_id": "request_123456789",
            "text": "hello",
            "audio": "base64",
        },
        {"operation": "interrupt", "id": call_id, "expected_revision": 0},
        {
            "operation": "start",
            "request_id": "short",
            "conversationId": "ses_source",
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "calls", io.BytesIO(json.dumps(payload).encode()), client
                )


@pytest.mark.parametrize(
    ("payload", "path", "forwarded", "execution"),
    [
        (
            {
                "operation": "save",
                "id": "example",
                "expected_revision": 0,
                "document": editor_document(),
            },
            "/v2/owner/editor-drafts/example",
            {"expected_revision": 0, "document": editor_document()},
            False,
        ),
        (
            {
                "operation": "publish",
                "id": "example",
                "expected_revision": 2,
                "expected_publication_version": 1,
                "authorization": "owner_confirmation",
            },
            "/v2/owner/editor-drafts/example/publish",
            {
                "expected_revision": 2,
                "expected_publication_version": 1,
                "authorization": "owner_confirmation",
            },
            False,
        ),
        (
            {
                "operation": "access",
                "id": "example",
                "version": 1,
                "digest": "b" * 64,
                "enabled": True,
            },
            "/v2/owner/editor-drafts/example/access",
            {"version": 1, "digest": "b" * 64, "enabled": True},
            True,
        ),
        (
            {
                "operation": "run",
                "id": "example",
                "version": 1,
                "digest": "b" * 64,
                "action_id": "editor_" + "c" * 32,
                "args": {"topic": "bounded"},
            },
            "/v2/owner/editor-drafts/example/runs",
            {
                "version": 1,
                "digest": "b" * 64,
                "action_id": "editor_" + "c" * 32,
                "args": {"topic": "bounded"},
            },
            True,
        ),
    ],
)
def test_tool_apply_keeps_owner_and_execution_channels_separate(
    payload, path, forwarded, execution
):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"status": "recorded"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "tools", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["status"] == "recorded"
    request = seen[0]
    assert request.url == httpx.URL("http://toolgate.test" + path)
    assert owner_editor_allowed("POST", path, execution=execution)
    assert json.loads(request.content) == forwarded
    assert request.headers["x-toolgate-owner-key"] == config().owner_key
    if execution:
        assert request.headers["x-toolgate-execution-key"] == config().toolgate_execution_key
    else:
        assert "x-toolgate-execution-key" not in request.headers
    assert "x-pi-owner-key" not in request.headers


def test_tool_apply_rejects_unreviewed_shapes_before_network():
    invalid = [
        {"operation": "delete", "id": "example"},
        {
            "operation": "save",
            "id": "example",
            "expected_revision": 0,
            "document": editor_document("different"),
        },
        {
            "operation": "publish",
            "id": "example",
            "expected_revision": 1,
            "expected_publication_version": 0,
            "authorization": "admin",
        },
        {
            "operation": "access",
            "id": "example",
            "version": 1,
            "digest": "not-a-digest",
            "enabled": True,
        },
        {
            "operation": "run",
            "id": "example",
            "version": 1,
            "digest": "b" * 64,
            "action_id": "random",
            "args": {},
        },
        {
            "operation": "run",
            "id": "example",
            "version": 1,
            "digest": "b" * 64,
            "action_id": "editor_" + "c" * 32,
            "args": {"large": "x" * 32769},
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "tools", io.BytesIO(json.dumps(payload).encode()), client
                )


def test_tool_execution_apply_requires_both_scoped_credentials_before_network():
    payload = {
        "operation": "access",
        "id": "example",
        "version": 1,
        "digest": "b" * 64,
        "enabled": True,
    }
    missing = Config(
        "https://conker.test",
        "/auth/auth.db",
        "http://pi.test",
        "runtime-" + "r" * 32,
        "http://toolgate.test",
        "owner-" + "o" * 32,
        pi_owner_key="control-" + "c" * 32,
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(MutationError, match="editor execution is not configured"),
    ):
        apply_resource(missing, "tools", io.BytesIO(json.dumps(payload).encode()), client)


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
def test_agent_apply_resolves_strict_operation_to_fixed_owner_route(
    payload, path, forwarded
):
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
                apply_resource(
                    config(), "agents", io.BytesIO(json.dumps(payload).encode()), client
                )


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
def test_team_apply_changes_configuration_without_exposing_execution(
    payload, path, forwarded
):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1, "revision": 5})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "teams", io.BytesIO(json.dumps(payload).encode()), client
        )

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
                apply_resource(
                    config(), "teams", io.BytesIO(json.dumps(payload).encode()), client
                )


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
def test_job_apply_matches_connected_state_and_run_operations(
    payload, path, forwarded, status
):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(status, json={"schemaVersion": 1, "status": "ready"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "jobs", io.BytesIO(json.dumps(payload).encode()), client
        )

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
                apply_resource(
                    config(), "jobs", io.BytesIO(json.dumps(payload).encode()), client
                )


def test_memory_forget_apply_preserves_review_revision_and_request_identity():
    payload = {
        "operation": "forget",
        "request_id": "forget_1234567890abcdef",
        "memory_id": "memory.important:1",
        "expected_revision": 8,
    }
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "requestId": payload["request_id"],
                "memoryId": payload["memory_id"],
                "status": "forgotten",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "memory", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["status"] == "forgotten"
    assert seen[0].url.path == "/memory/forget"
    assert owner_allowed("POST", seen[0].url.path)
    assert json.loads(seen[0].content) == {
        "request_id": payload["request_id"],
        "memory_id": payload["memory_id"],
        "expected_revision": 8,
    }


def test_memory_apply_rejects_generic_delete_and_missing_review_revision():
    invalid = [
        {"operation": "delete", "memory_id": "memory.important:1"},
        {
            "operation": "forget",
            "request_id": "forget_1234567890abcdef",
            "memory_id": "memory.important:1",
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "memory", io.BytesIO(json.dumps(payload).encode()), client
                )


@pytest.mark.parametrize("decision", ["accept", "decline", "never"])
def test_proposal_decision_uses_runtime_channel_and_grants_no_action(decision):
    payload = {"operation": "decide", "id": "proposal_123", "decision": decision}
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"id": payload["id"], "state": decision})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "proposals", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["id"] == payload["id"]
    request = seen[0]
    assert request.url.path == "/proposals/proposal_123/decision"
    assert runtime_allowed("POST", request.url.path)
    assert json.loads(request.content) == {"decision": decision}
    assert request.headers["x-pi-gateway-key"] == config().pi_key
    assert "x-pi-owner-key" not in request.headers


def test_proposal_apply_rejects_execution_and_unknown_decisions_before_network():
    invalid = [
        {"operation": "execute", "id": "proposal_123"},
        {"operation": "decide", "id": "proposal_123", "decision": "run"},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "proposals", io.BytesIO(json.dumps(payload).encode()), client
                )


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {
                "operation": "create",
                "request_id": "task_1234567890abcdef",
                "session_id": "ses_1234567890abcdef",
                "outcome": "Prepare the release.",
                "criteria": ["Tests pass"],
                "parent_task_id": None,
                "run_ids": [],
            },
            "/tasks",
            {
                "request_id": "task_1234567890abcdef",
                "session_id": "ses_1234567890abcdef",
                "outcome": "Prepare the release.",
                "criteria": ["Tests pass"],
                "parent_task_id": None,
                "run_ids": [],
            },
        ),
        (
            {
                "operation": "update",
                "id": "tsk_" + "1" * 32,
                "expected_revision": 2,
                "outcome": "Prepare the release safely.",
                "criteria": ["Tests pass", "Evidence retained"],
                "parent_task_id": None,
                "run_ids": [],
            },
            "/tasks/tsk_" + "1" * 32 + "/update",
            {
                "expected_revision": 2,
                "outcome": "Prepare the release safely.",
                "criteria": ["Tests pass", "Evidence retained"],
                "parent_task_id": None,
                "run_ids": [],
            },
        ),
        (
            {
                "operation": "transition",
                "id": "tsk_" + "1" * 32,
                "expected_revision": 3,
                "status": "completed",
                "note": "Acceptance passed.",
                "completed_criterion_ids": ["criterion_1"],
            },
            "/tasks/tsk_" + "1" * 32 + "/transition",
            {
                "expected_revision": 3,
                "status": "completed",
                "note": "Acceptance passed.",
                "completed_criterion_ids": ["criterion_1"],
            },
        ),
        (
            {
                "operation": "archive",
                "id": "tsk_" + "1" * 32,
                "expected_revision": 4,
                "archived": True,
            },
            "/tasks/tsk_" + "1" * 32 + "/archive",
            {"expected_revision": 4, "archived": True},
        ),
    ],
)
def test_task_apply_uses_typed_runtime_operations(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"id": "tsk_" + "1" * 32, "revision": 5})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "tasks", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 5
    request = seen[0]
    assert request.url.path == path
    assert runtime_allowed("POST", path)
    assert json.loads(request.content) == forwarded
    assert request.headers["x-pi-gateway-key"] == config().pi_key
    assert "x-pi-owner-key" not in request.headers


def test_task_apply_rejects_invalid_transition_and_unstable_request_before_network():
    invalid = [
        {
            "operation": "transition",
            "id": "tsk_" + "1" * 32,
            "expected_revision": 1,
            "status": "deleted",
            "note": "No.",
        },
        {
            "operation": "create",
            "request_id": "short",
            "session_id": "session",
            "outcome": "Outcome",
            "criteria": ["Done"],
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "tasks", io.BytesIO(json.dumps(payload).encode()), client
                )


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {
                "operation": "create",
                "title": "Release notes",
                "content": {"kind": "markdown", "text": "Ready."},
                "taskId": None,
            },
            "/artifacts",
            {
                "title": "Release notes",
                "content": {"kind": "markdown", "text": "Ready."},
            },
        ),
        (
            {
                "operation": "from_message",
                "title": "Answer",
                "sessionId": "ses_" + "2" * 16,
                "messageId": "msg_" + "3" * 16,
                "taskId": None,
            },
            "/artifacts/from-message",
            {
                "title": "Answer",
                "sessionId": "ses_" + "2" * 16,
                "messageId": "msg_" + "3" * 16,
            },
        ),
        (
            {
                "operation": "append",
                "id": "artifact_" + "4" * 32,
                "expected_revision": 2,
                "content": {"kind": "code", "text": "print('ok')", "language": "python"},
                "title": None,
                "note": "Verified",
                "preserveCitations": True,
            },
            "/artifacts/artifact_" + "4" * 32 + "/versions",
            {
                "expected_revision": 2,
                "content": {"kind": "code", "text": "print('ok')", "language": "python"},
                "note": "Verified",
                "preserveCitations": True,
            },
        ),
        (
            {
                "operation": "restore",
                "id": "artifact_" + "4" * 32,
                "expected_revision": 3,
                "version": 1,
            },
            "/artifacts/artifact_" + "4" * 32 + "/restore",
            {"expected_revision": 3, "version": 1},
        ),
        (
            {
                "operation": "archive",
                "id": "artifact_" + "4" * 32,
                "expected_revision": 4,
                "archived": True,
            },
            "/artifacts/artifact_" + "4" * 32 + "/archive",
            {"expected_revision": 4, "archived": True},
        ),
    ],
)
def test_artifact_apply_preserves_inert_versioned_operations(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1, "revision": 5})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "artifacts", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 5
    request = seen[0]
    assert request.url.path == path
    assert owner_allowed("POST", path)
    assert json.loads(request.content) == forwarded


def test_artifact_apply_rejects_execution_download_and_unsafe_media_before_network():
    invalid = [
        {"operation": "execute", "id": "artifact_" + "4" * 32},
        {"operation": "download", "id": "artifact_" + "4" * 32},
        {
            "operation": "create",
            "title": "Unsafe",
            "content": {
                "kind": "media",
                "mediaType": "image",
                "url": "file:///etc/passwd",
                "description": "No",
            },
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "artifacts", io.BytesIO(json.dumps(payload).encode()), client
                )


def test_session_apply_uses_revision_checked_owner_settings_operation():
    payload = {
        "operation": "update",
        "id": "ses_1234567890abcdef",
        "expected_revision": 3,
        "settings": {
            "agentId": "companion",
            "privacy": {"memoryDisabled": True, "harnessDisabled": True},
            "projectId": None,
            "projectSources": [],
            "presentationMode": "focus",
        },
    }
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"revision": 4, "settings": payload["settings"]})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "sessions", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 4
    request = seen[0]
    assert request.url.path == "/sessions/ses_1234567890abcdef/settings"
    assert owner_allowed("POST", request.url.path)
    assert json.loads(request.content) == {
        "expected_revision": 3,
        "settings": payload["settings"],
    }
    assert request.headers["x-pi-owner-key"] == config().pi_owner_key


def test_session_apply_rejects_missing_revision_invalid_sources_and_authority():
    base = {
        "operation": "update",
        "id": "ses_1234567890abcdef",
        "expected_revision": 3,
        "settings": {
            "agentId": "companion",
            "privacy": {"memoryDisabled": False, "harnessDisabled": False},
            "projectId": None,
            "projectSources": [],
            "presentationMode": None,
        },
    }
    invalid = [
        {key: value for key, value in base.items() if key != "expected_revision"},
        base
        | {
            "settings": base["settings"]
            | {
                "projectSources": [
                    {"kind": "conversation", "sessionId": "ses_1234567890abcdef"}
                ]
            }
        },
        base | {"authority": "admin"},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "sessions", io.BytesIO(json.dumps(payload).encode()), client
                )


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
            apply_resource(
                config(), "models", io.BytesIO(b"x" * (MAX_REQUEST_BYTES + 1)), client
            )


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
