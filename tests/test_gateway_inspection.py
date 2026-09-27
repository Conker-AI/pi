from __future__ import annotations

import json

import httpx
import pytest

from gateway.api import Config
from gateway.inspection import (
    INSPECTIONS,
    MAX_RESPONSE_BYTES,
    InspectionError,
    inspect_resource,
    resources,
)
from gateway.toolgate_contract import owner_editor_allowed, owner_request_allowed
from pi.browser_contract import owner_allowed, runtime_allowed


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


def test_all_inspection_resources_use_fixed_allowlisted_get_routes():
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"resource": request.url.path})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        for name in resources():
            result = inspect_resource(config(), name, client)
            assert result == {"resource": INSPECTIONS[name].path}

    assert len(seen) == len(INSPECTIONS)
    for name, request in zip(resources(), seen, strict=True):
        operation = INSPECTIONS[name]
        assert request.method == "GET"
        base = (
            "http://toolgate.test"
            if operation.authority == "toolgate-owner"
            else "http://pi.test"
        )
        assert request.url.copy_with(query=None) == httpx.URL(base + operation.path)
        assert dict(request.url.params) == dict(operation.query)
        assert request.headers["accept"] == "application/json"
        if operation.authority == "owner":
            assert owner_allowed("GET", operation.path)
            assert request.headers["x-pi-owner-key"] == config().pi_owner_key
            assert "x-pi-gateway-key" not in request.headers
        elif operation.authority == "runtime":
            assert runtime_allowed("GET", operation.path)
            assert request.headers["x-pi-gateway-key"] == config().pi_key
            assert "x-pi-owner-key" not in request.headers
        else:
            assert owner_request_allowed("GET", operation.path) or owner_editor_allowed(
                "GET", operation.path
            )
            assert request.headers["x-toolgate-owner-key"] == config().owner_key
            assert "x-toolgate-execution-key" not in request.headers
            assert "x-pi-owner-key" not in request.headers
            assert "x-pi-gateway-key" not in request.headers


def test_unknown_resource_and_missing_credentials_fail_before_network():
    calls = 0

    def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(InspectionError, match="Unknown inspection resource"):
            inspect_resource(config(), "https://attacker.invalid", client)
        missing = Config("https://conker.test", "/auth/auth.db", "http://pi.test", "r" * 32)
        with pytest.raises(InspectionError, match="owner inspection is not configured"):
            inspect_resource(missing, "agents", client)
    assert calls == 0


def test_session_settings_inspection_resolves_one_validated_identity():
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "revision": 2,
                "settings": {
                    "agentId": "companion",
                    "privacy": {"memoryDisabled": False, "harnessDisabled": True},
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = inspect_resource(config(), "session-settings:ses_1234567890abcdef", client)

    assert result["revision"] == 2
    assert seen[0].url.path == "/sessions/ses_1234567890abcdef/settings"
    assert owner_allowed("GET", seen[0].url.path)
    assert seen[0].headers["x-pi-owner-key"] == config().pi_owner_key


def test_approval_detail_inspection_resolves_one_validated_identity():
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "req_123", "status": "pending"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = inspect_resource(config(), "approval:req_123", client)

    assert result == {"id": "req_123", "status": "pending"}
    assert seen[0].url == httpx.URL("http://toolgate.test/v2/owner/requests/req_123")
    assert owner_request_allowed("GET", seen[0].url.path)
    assert seen[0].headers["x-toolgate-owner-key"] == config().owner_key
    assert "x-toolgate-execution-key" not in seen[0].headers


def test_submission_inspection_resolves_one_validated_runtime_identity():
    request_id = "request_1234567890abcdef"
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"request_id": request_id, "status": "running"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = inspect_resource(config(), f"submission:{request_id}", client)

    assert result == {"request_id": request_id, "status": "running"}
    assert seen[0].url == httpx.URL(
        f"http://pi.test/turn-submissions/{request_id}"
    )
    assert runtime_allowed("GET", seen[0].url.path)
    assert seen[0].headers["x-pi-gateway-key"] == config().pi_key
    assert "x-pi-owner-key" not in seen[0].headers


@pytest.mark.parametrize(
    "resource",
    [
        "session-settings:",
        "session-settings:../health",
        "session-settings:https://attacker.invalid",
        "session-settings:" + "a" * 129,
    ],
)
def test_session_settings_inspection_rejects_invalid_identity_before_network(resource):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(InspectionError, match="Unknown inspection resource"),
    ):
        inspect_resource(config(), resource, client)


@pytest.mark.parametrize(
    "resource",
    [
        "approval:",
        "approval:../health",
        "approval:https://attacker.invalid",
        "approval:" + "a" * 129,
    ],
)
def test_approval_inspection_rejects_invalid_identity_before_network(resource):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(InspectionError, match="Unknown inspection resource"),
    ):
        inspect_resource(config(), resource, client)


def test_approval_inspection_requires_toolgate_owner_key_before_network():
    missing = Config(
        "https://conker.test",
        "/auth/auth.db",
        "http://pi.test",
        "runtime-" + "r" * 32,
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(InspectionError, match="ToolGate owner inspection"),
    ):
        inspect_resource(missing, "approvals", client)


@pytest.mark.parametrize(
    "resource",
    [
        "submission:",
        "submission:too-short",
        "submission:../health________",
        "submission:https_attacker_invalid/path",
        "submission:" + "a" * 129,
    ],
)
def test_submission_inspection_rejects_invalid_identity_before_network(resource):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(InspectionError, match="Unknown inspection resource"),
    ):
        inspect_resource(config(), resource, client)


@pytest.mark.parametrize(
    ("resource", "path"),
    [
        (
            "file-listing:listing_1234567890abcdef",
            "/system/files/listings/listing_1234567890abcdef",
        ),
        (
            "inventory:inventory_1234567890abcdef",
            "/system/inventory/inventory_1234567890abcdef",
        ),
    ],
)
def test_read_only_system_detail_inspection_uses_owner_route(resource, path):
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"state": "complete"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = inspect_resource(config(), resource, client)

    assert result == {"state": "complete"}
    assert seen[0].url == httpx.URL("http://pi.test" + path)
    assert owner_allowed("GET", path)
    assert seen[0].headers["x-pi-owner-key"] == config().pi_owner_key
    assert "x-pi-gateway-key" not in seen[0].headers


@pytest.mark.parametrize(
    "resource",
    [
        "file-listing:short",
        "file-listing:../secret_______",
        "inventory:short",
        "inventory:../daemon________",
        "inventory:" + "a" * 101,
    ],
)
def test_read_only_system_detail_rejects_invalid_identity_before_network(resource):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(InspectionError, match="Unknown inspection resource"),
    ):
        inspect_resource(config(), resource, client)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(302, headers={"location": "https://attacker.invalid"}), "HTTP 302"),
        (httpx.Response(200, text="not-json"), "non-JSON"),
        (httpx.Response(200, content=b"[]", headers={"content-type": "application/json"}), "unsupported"),
        (httpx.Response(403, text="secret upstream detail"), "HTTP 403"),
    ],
)
def test_inspection_rejects_redirects_malformed_shapes_and_denials(response, message):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as client,
        pytest.raises(InspectionError, match=message) as failure,
    ):
        inspect_resource(config(), "agents", client)
    assert "secret upstream detail" not in str(failure.value)


def test_inspection_bounds_streamed_response_bytes():
    body = json.dumps({"value": "x" * MAX_RESPONSE_BYTES}).encode()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        )
    ) as client, pytest.raises(InspectionError, match="exceeded 8 MiB"):
        inspect_resource(config(), "agents", client)


def test_character_inspection_uses_the_character_response_envelope():
    declared = 9 * 1024 * 1024
    response = httpx.Response(
        200,
        content=b"{}",
        headers={"content-type": "application/json", "content-length": str(declared)},
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as client:
        assert inspect_resource(config(), "character", client) == {}


@pytest.mark.parametrize(
    ("resource", "path"),
    [
        (
            "call:call_" + "a" * 32,
            "/calls/browser/call_" + "a" * 32,
        ),
        (
            "active-call:ses_source_123",
            "/calls/browser/active/ses_source_123",
        ),
    ],
)
def test_call_inspection_resolves_typed_only_owner_routes(resource, path):
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"execution": "typed-turns-only"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = inspect_resource(config(), resource, client)

    assert result == {"execution": "typed-turns-only"}
    assert seen[0].url == httpx.URL("http://pi.test" + path)
    assert owner_allowed("GET", path)
    assert seen[0].headers["x-pi-owner-key"] == config().pi_owner_key


@pytest.mark.parametrize(
    "resource",
    [
        "call:call_short",
        "call:call_" + "A" * 32,
        "call:../health",
        "active-call:",
        "active-call:../session",
        "active-call:" + "a" * 201,
    ],
)
def test_call_inspection_rejects_invalid_identity_before_network(resource):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(InspectionError, match="Unknown inspection resource"),
    ):
        inspect_resource(config(), resource, client)


@pytest.mark.parametrize(
    ("resource", "path", "query", "execution"),
    [
        (
            "tool-draft:example",
            "/v2/owner/editor-drafts/example",
            {},
            False,
        ),
        (
            "tool-validation:example",
            "/v2/owner/editor-drafts/example/validation",
            {},
            False,
        ),
        (
            "tool-publications:example",
            "/v2/owner/editor-drafts/example/publications",
            {},
            False,
        ),
        (
            "tool-runs:example",
            "/v2/owner/editor-drafts/example/runs",
            {},
            True,
        ),
        (
            "tool-access:example:2:" + "b" * 64,
            "/v2/owner/editor-drafts/example/access",
            {"version": "2", "digest": "b" * 64},
            True,
        ),
    ],
)
def test_tool_inspection_keeps_owner_and_execution_channels_separate(
    resource, path, query, execution
):
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "visible"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = inspect_resource(config(), resource, client)

    assert result == {"status": "visible"}
    request = seen[0]
    assert request.url.path == path
    assert dict(request.url.params) == query
    assert owner_editor_allowed("GET", path, execution=execution)
    assert request.headers["x-toolgate-owner-key"] == config().owner_key
    if execution:
        assert request.headers["x-toolgate-execution-key"] == config().toolgate_execution_key
    else:
        assert "x-toolgate-execution-key" not in request.headers


@pytest.mark.parametrize(
    "resource",
    [
        "tool-draft:../secret",
        "tool-validation:1invalid",
        "tool-publications:" + "a" * 65,
        "tool-runs:bad.id",
        "tool-access:example:0:" + "b" * 64,
        "tool-access:example:1:not-a-digest",
    ],
)
def test_tool_inspection_rejects_invalid_identity_before_network(resource):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(InspectionError, match="Unknown inspection resource"),
    ):
        inspect_resource(config(), resource, client)


def test_tool_execution_inspection_requires_both_credentials_before_network():
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
        pytest.raises(InspectionError, match="editor execution is not configured"),
    ):
        inspect_resource(missing, "tool-runs:example", client)
