"""Gateway writes for the owner's character, calls and tools."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from gateway.api import Config
from gateway.mutation import MutationError, apply_resource
from gateway.toolgate_contract import owner_editor_allowed
from pi.api.browser_contract import owner_allowed
from tests.test_characters import profile as character_profile
from tests.test_gateway_mutation import config, editor_document


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
        assert (
            apply_resource(config(), "character", io.BytesIO(json.dumps(payload).encode()), client)
            == {}
        )


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
        result = apply_resource(config(), "calls", io.BytesIO(json.dumps(payload).encode()), client)

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
                apply_resource(config(), "calls", io.BytesIO(json.dumps(payload).encode()), client)


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
        result = apply_resource(config(), "tools", io.BytesIO(json.dumps(payload).encode()), client)

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
                apply_resource(config(), "tools", io.BytesIO(json.dumps(payload).encode()), client)


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
