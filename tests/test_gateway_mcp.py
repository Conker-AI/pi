"""MCP owner operations retain session, exact proof and scoped service boundaries."""

import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import COOKIE, Config, create_app
from gateway.store import AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic MCP owner verification phrase"
OWNER_KEY = "mcp-owner-" + "o" * 32
EXECUTION_KEY = "tgx_mcp_" + "e" * 32
BASE = "/api/owner/mcp"
DEFINITION = {
    "id": "mcp.echo",
    "name": "Echo",
    "description": "Return supplied text",
    "url": "https://mcp.example.test/mcp",
    "tool_name": "echo",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    "schema_hash": "a" * 64,
}
STATE = {"enabled": True, "expected_digest": "b" * 64}
WRITES = [
    (BASE + "/discover", {"url": DEFINITION["url"]}),
    (BASE + "/import", DEFINITION),
    (BASE + "/tools/mcp.echo/state", STATE),
    (BASE + "/tools/mcp.echo/access", STATE),
]


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen, mode = [], {"response": "ok"}

    def upstream(request):
        seen.append(request)
        if mode["response"] == "timeout":
            raise httpx.ReadTimeout("upstream-private-secret")
        if mode["response"] == "redirect":
            return httpx.Response(307, headers={"Location": "https://elsewhere.test/private"})
        if mode["response"] == "malformed":
            return httpx.Response(200, text="upstream-private-secret")
        return httpx.Response(
            200,
            json={"ok": True},
            headers={
                "Set-Cookie": "upstream_auth=untrusted; Path=/",
                "X-Private": "upstream-private-secret",
            },
        )

    config = Config(
        ORIGIN,
        str(auth.path),
        "http://pi.test",
        "r" * 32,
        toolgate_url="http://toolgate.test",
        owner_key=OWNER_KEY,
        toolgate_execution_key=EXECUTION_KEY,
    )
    app = create_app(config, store=auth, transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        yield app, client, headers, seen, mode


def proof(client, headers, path, body):
    response = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": body}},
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Conker-Verification": response.json()["verification_token"]}


@pytest.mark.parametrize("path,body", WRITES)
def test_every_write_requires_csrf_and_exact_single_use_proof(boundary, path, body):
    _, client, headers, seen, _ = boundary
    for invalid_headers in (
        {},
        {**headers, "Origin": "https://elsewhere.test"},
        {**headers, "X-CSRF-Token": "wrong"},
    ):
        assert client.post(path, json=body, headers=invalid_headers).status_code == 403
    assert client.post(path, json=body, headers=headers).status_code == 428
    checked = proof(client, headers, path, body)
    changed = (
        {**body, "enabled": False}
        if "enabled" in body
        else {**body, "url": "https://other.test/mcp"}
    )
    assert client.post(path, json=changed, headers=checked).status_code == 428
    assert not seen
    response = client.post(path, json=body, headers=checked)
    assert response.status_code == 200
    assert client.post(path, json=body, headers=checked).status_code == 428
    assert len(seen) == 1
    assert seen[0].url == "http://toolgate.test" + path.replace("/api/owner/", "/v2/owner/")
    assert json.loads(seen[0].content) == body
    assert seen[0].headers["x-toolgate-owner-key"] == OWNER_KEY
    assert ("x-toolgate-execution-key" in seen[0].headers) == path.endswith("/access")
    assert "cookie" not in seen[0].headers and "x-conker-verification" not in seen[0].headers
    assert PASSWORD not in seen[0].content.decode()
    assert "set-cookie" not in response.headers and "x-private" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_read_and_access_credentials_are_server_owned_and_cookies_never_propagate(boundary):
    _, client, _, seen, _ = boundary
    original = client.cookies.get(COOKIE)
    spoofed = {
        "X-ToolGate-Owner-Key": "browser-owner",
        "X-ToolGate-Execution-Key": "browser-runtime",
        "X-ToolGate-Key": "browser-admin",
        "Authorization": "Bearer browser",
    }
    assert client.get(BASE + "/tools", headers=spoofed).status_code == 200
    assert client.get(BASE + "/tools/mcp.echo/access", headers=spoofed).status_code == 200
    assert client.cookies.get(COOKIE) == original
    for request in seen:
        assert request.headers["x-toolgate-owner-key"] == OWNER_KEY
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert "x-toolgate-key" not in request.headers
    assert "x-toolgate-execution-key" not in seen[0].headers
    assert seen[1].headers["x-toolgate-execution-key"] == EXECUTION_KEY


@pytest.mark.parametrize("path,body", WRITES)
def test_unknown_fields_are_rejected_before_forwarding(boundary, path, body):
    _, client, headers, seen, _ = boundary
    assert (
        client.post(
            path, json={**body, "headers": {"Authorization": "secret"}}, headers=headers
        ).status_code
        == 422
    )
    assert not seen


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", BASE + "/tools", None),
        ("GET", BASE + "/tools/mcp.echo/access", None),
        *(("POST", path, body) for path, body in WRITES),
    ],
)
def test_queries_and_signed_out_requests_never_reach_toolgate(boundary, method, path, body):
    _, client, headers, seen, _ = boundary
    assert (
        client.request(method, path + "?identity=mcp.other", json=body, headers=headers).status_code
        == 422
    )
    client.cookies.clear()
    assert client.request(method, path, json=body, headers=headers).status_code == 401
    assert not seen


@pytest.mark.parametrize("identity", ["shell", "mcp.AA", "mcp.a", "mcp.a_b", "mcp." + "x" * 76])
def test_invalid_tool_id_cannot_be_verified_or_forwarded(boundary, identity):
    _, client, headers, seen, _ = boundary
    path = BASE + f"/tools/{identity}/access"
    assert client.get(path).status_code == 422
    assert client.post(path, json=STATE, headers=headers).status_code == 422
    assert (
        client.post(
            "/auth/verify",
            headers=headers,
            json={
                "password": PASSWORD,
                "operation": {"method": "POST", "path": path, "body": STATE},
            },
        ).status_code
        == 422
    )
    assert not seen


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://host.test/mcp\n"},
        {"url": "https://host.test\\mcp"},
        {"url": "http://host.test/mcp"},
        {"url": "https://owner:secret@host.test/mcp"},
        {"url": "https://host.test:8443/mcp"},
        {"url": "https://host.test/mcp?token=private"},
        {"url": 42},
        {"secret_ref": "../private"},
        {"secret_ref": "lowercase"},
        {"id": "mcp.x"},
        {"name": " "},
        {"name": "x" * 121},
        {"description": ""},
        {"tool_name": "bad/name"},
        {"input_schema": []},
        {"input_schema": {"type": "string"}},
        {"input_schema": {"type": "object", "description": "x" * 32768}},
        {"schema_hash": 1},
        {"schema_hash": "ABC"},
    ],
)
def test_import_rejects_invalid_bounded_fields(boundary, changes):
    _, client, headers, seen, _ = boundary
    assert (
        client.post(BASE + "/import", json={**DEFINITION, **changes}, headers=headers).status_code
        == 422
    )
    assert not seen


@pytest.mark.parametrize(
    "body",
    [
        {"enabled": "true", "expected_digest": "b" * 64},
        {"enabled": True, "expected_digest": 1},
        {"enabled": True},
        {"expected_digest": "b" * 64},
    ],
)
def test_state_requires_strict_boolean_and_digest(boundary, body):
    _, client, headers, seen, _ = boundary
    assert (
        client.post(BASE + "/tools/mcp.echo/state", json=body, headers=headers).status_code == 422
    )
    assert not seen


@pytest.mark.parametrize(
    "failure,expected", [("timeout", 503), ("redirect", 502), ("malformed", 502)]
)
def test_upstream_failure_is_not_retried_or_leaked(boundary, failure, expected):
    _, client, headers, seen, mode = boundary
    path = BASE + "/tools/mcp.echo/access"
    checked = proof(client, headers, path, STATE)
    mode["response"] = failure
    response = client.post(path, json=STATE, headers=checked)
    assert response.status_code == expected
    assert "upstream-private-secret" not in response.text and "elsewhere.test" not in response.text
    assert client.post(path, json=STATE, headers=checked).status_code == 428
    assert len(seen) == 1


def test_access_proof_cannot_change_route_and_missing_credentials_fail_closed(boundary):
    app, client, headers, seen, _ = boundary
    path = BASE + "/tools/mcp.echo/access"
    checked = proof(client, headers, path, STATE)
    assert (
        client.post(BASE + "/tools/mcp.echo/state", json=STATE, headers=checked).status_code == 428
    )
    app.state.config = replace(app.state.config, toolgate_execution_key="")
    assert client.get(path).status_code == 503
    assert client.post(path, json=STATE, headers=checked).status_code == 503
    app.state.config = replace(app.state.config, toolgate_execution_key=EXECUTION_KEY, owner_key="")
    assert client.get(BASE + "/tools").status_code == 503
    assert not seen


def test_duplicate_json_keys_and_large_bodies_are_refused(boundary):
    _, client, headers, seen, _ = boundary
    headers = {**headers, "Content-Type": "application/json"}
    assert (
        client.post(
            BASE + "/discover",
            content='{"url":"https://one.test","url":"https://two.test"}',
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(BASE + "/discover", json={"url": "x" * 65536}, headers=headers).status_code
        == 413
    )
    assert not seen
