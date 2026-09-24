import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api import COOKIE, Config, create_app
from gateway.store import AuthStore

PASSWORD = "four quiet trees beside school"
ORIGIN = "https://localhost:8050"
RUNTIME = "runtime-credential-" + "r" * 32
OWNER = "owner-credential-" + "o" * 32


@pytest.fixture
def gateway(tmp_path):
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={"memory": {"ingestion": "pending"}},
            headers={"Set-Cookie": "service_secret=bad", "X-Pi-Key": "bad"},
        )

    config = Config(
        ORIGIN,
        str(auth.path),
        "http://pi:8050",
        RUNTIME,
        owner_key=OWNER,
        toolgate_execution_key="tgx_" + "e" * 40,
    )
    with TestClient(
        create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=ORIGIN
    ) as client:
        yield client, auth, seen


def sign_in(client):
    start = client.get("/auth/session")
    before = client.cookies.get(COOKIE)
    result = client.post(
        "/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": start.json()["csrf_token"]},
    )
    assert result.status_code == 200, result.text
    assert client.cookies.get(COOKIE) != before
    return {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}


def verified_headers(client, headers, path, body):
    result = client.post(
        "/auth/verify",
        headers=headers,
        json={
            "password": PASSWORD,
            "operation": {"method": "POST", "path": path, "body": body},
        },
    )
    assert result.status_code == 200, result.text
    return {**headers, "X-Conker-Verification": result.json()["verification_token"]}


def test_editor_draft_write_requires_exact_one_use_proof(gateway):
    client, _, seen = gateway
    path = "/api/owner/editor-drafts/example"
    assert client.get(path).status_code == 401
    headers = sign_in(client)
    body = {"expected_revision": 0, "document": {"id": "example"}}
    assert client.post(path, json=body, headers=headers).status_code == 428
    verified = verified_headers(client, headers, path, body)
    assert (
        client.post(path, json={**body, "expected_revision": 1}, headers=verified).status_code
        == 428
    )
    assert client.post(path, json=body, headers=verified).status_code == 200
    assert seen[-1].url.path == "/v2/owner/editor-drafts/example"
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert "X-ToolGate-Execution-Key" not in seen[-1].headers
    assert "cookie" not in seen[-1].headers
    assert client.post(path, json=body, headers=verified).status_code == 428
    assert len(seen) == 1


def test_editor_draft_routes_do_not_widen_to_execution_or_arbitrary_paths(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    for suffix in ("?limit=0", "?limit=101", "?limit=2&limit=3", "?secret=x", "?after=../vault"):
        assert client.get("/api/owner/editor-drafts" + suffix).status_code == 422
    assert not seen
    assert client.get("/api/owner/editor-drafts?limit=2&after=a").status_code == 200
    assert str(seen[-1].url).endswith("/v2/owner/editor-drafts?limit=2&after=a")
    for path in ("/api/owner/editor-drafts/example/delete", "/api/owner/editor-drafts/example/run"):
        result = client.post(
            "/auth/verify",
            headers=headers,
            json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": {}}},
        )
        assert result.status_code == 422


def test_editor_publication_requires_exact_one_use_proof_and_owner_channel(gateway):
    client, _, seen = gateway
    path = "/api/owner/editor-drafts/example/publish"
    headers = sign_in(client)
    body = {
        "expected_revision": 2,
        "expected_publication_version": 1,
        "authorization": "owner_confirmation",
    }
    assert client.post(path, json=body, headers=headers).status_code == 428
    verified = verified_headers(client, headers, path, body)
    assert (
        client.post(path, json={**body, "authorization": "auto"}, headers=verified).status_code
        == 428
    )
    assert client.post(path, json=body, headers=verified).status_code == 200
    assert seen[-1].url.path == "/v2/owner/editor-drafts/example/publish"
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert "X-ToolGate-Execution-Key" not in seen[-1].headers
    assert client.post(path, json=body, headers=verified).status_code == 428
    for operation in ("publications", "validation"):
        assert client.get(f"/api/owner/editor-drafts/example/{operation}").status_code == 200
        assert (
            client.get(f"/api/owner/editor-drafts/example/{operation}?secret=x").status_code == 422
        )


def test_editor_catalogue_is_a_narrow_read_only_owner_route(gateway):
    client, _, seen = gateway
    path = "/api/owner/editor-capabilities"
    assert client.get(path).status_code == 401
    sign_in(client)
    for suffix in ("?kind=secret", "?limit=0", "?q=a&q=b", "?after=../secret", "?extra=x"):
        assert client.get(path + suffix).status_code == 422
    assert seen == []
    assert client.get(path + "?kind=tool&q=echo&limit=10").status_code == 200
    assert seen[-1].url.path == "/v2/owner/editor-capabilities"
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert client.post(path, json={}).status_code == 405


@pytest.mark.parametrize(
    "operation,fields",
    [("access", {"enabled": True}), ("runs", {"action_id": "editor_" + "a" * 32, "args": {}})],
)
def test_editor_execution_operations_require_proof_and_both_host_credentials(
    gateway, operation, fields
):
    client, _, seen = gateway
    headers = sign_in(client)
    path = f"/api/owner/editor-drafts/example/{operation}"
    body = {"version": 1, "digest": "b" * 64, **fields}
    assert client.post(path, headers=headers, json=body).status_code == 428
    verified = verified_headers(client, headers, path, body)
    assert client.post(path, headers=verified, json={**body, "version": 2}).status_code == 428
    assert client.post(path, headers=verified, json=body).status_code == 200
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert seen[-1].headers["X-ToolGate-Execution-Key"] == "tgx_" + "e" * 40
    assert client.post(path, headers=verified, json=body).status_code == 428


def test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate(gateway):
    client, _, seen = gateway
    cookie = client.get("/auth/session").headers["set-cookie"]
    assert (
        COOKIE in cookie
        and "Secure" in cookie
        and "HttpOnly" in cookie
        and "SameSite=strict" in cookie
    )
    assert "Path=/" in cookie and "Domain=" not in cookie
    for header, key in (
        ("X-Pi-Key", RUNTIME),
        ("X-ToolGate-Execution-Key", OWNER),
        ("X-ToolGate-Owner-Key", OWNER),
    ):
        assert client.get("/api/owner/requests", headers={header: key}).status_code == 401
    assert not seen


def test_login_requires_csrf_and_exact_origin(gateway):
    client, _, seen = gateway
    csrf = client.get("/auth/session").json()["csrf_token"]
    for headers in (
        {},
        {"Origin": ORIGIN},
        {"Origin": "https://evil.invalid", "X-CSRF-Token": csrf},
        {"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
    ):
        assert (
            client.post("/auth/login", json={"password": PASSWORD}, headers=headers).status_code
            == 403
        )
    assert not seen


def test_proxy_keeps_credentials_separate_and_preserves_memory_status(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    result = client.post(
        "/api/pi/sessions",
        json={"title": "school"},
        headers={
            **verified_headers(client, headers, "/api/pi/sessions", {"title": "school"}),
            "X-Pi-Key": "injected",
            "X-ToolGate-Owner-Key": "injected",
        },
    )
    assert result.json() == {"memory": {"ingestion": "pending"}}
    assert "set-cookie" not in result.headers and "x-pi-key" not in result.headers
    assert seen[-1].headers["X-Pi-Gateway-Key"] == RUNTIME
    assert "X-ToolGate-Owner-Key" not in seen[-1].headers
    assert "X-Pi-Key" not in seen[-1].headers and "Cookie" not in seen[-1].headers
    client.get("/api/pi/sessions")
    assert "Cookie" not in seen[-1].headers
    result = client.post(
        "/api/owner/requests/req_1/decision",
        json={"status": "approved"},
        headers=verified_headers(
            client, headers, "/api/owner/requests/req_1/decision", {"status": "approved"}
        ),
    )
    assert result.status_code == 200
    assert str(seen[-1].url) == "http://toolgate-api:8010/v2/owner/requests/req_1/decision"
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert "X-Pi-Gateway-Key" not in seen[-1].headers
    assert OWNER not in result.text and RUNTIME not in result.text


def test_unsafe_operations_require_csrf_and_logout_revokes_the_cookie(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    for path, body in (
        ("/api/pi/sessions", {}),
        ("/api/owner/requests/r/decision", {"status": "approved"}),
        ("/auth/logout", {}),
        ("/auth/revoke-all", {}),
    ):
        assert client.post(path, json=body).status_code == 403
    assert not seen
    old = client.cookies.get(COOKIE)
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/pi/sessions", headers={"Cookie": f"{COOKIE}={old}"}).status_code == 401


def test_unknown_routes_and_forged_origins_never_reach_services(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    for path in ("/api/pi/admin", "/api/pi/v2/requests/r/decision", "/api/pi/sessions/a%2Fb"):
        assert client.post(path, json={}, headers=headers).status_code in {403, 404}
    assert client.get("/api/pi/sessions", headers={"Host": "evil.invalid"}).status_code == 400
    assert (
        client.get(
            "http://localhost:8050/auth/session", headers={"X-Forwarded-Proto": "https"}
        ).status_code
        == 400
    )
    assert not seen


def test_session_revocation_endpoint_and_expiry_block_proxy(gateway):
    client, auth, seen = gateway
    headers = sign_in(client)
    sessions = client.get("/auth/sessions").json()["results"]
    assert len(sessions) == 1 and "token_hash" not in sessions[0] and "csrf" not in sessions[0]
    assert (
        client.post(f"/auth/sessions/{sessions[0]['id']}/revoke", headers=headers).status_code
        == 200
    )
    assert client.get("/api/pi/sessions").status_code == 401
    sign_in(client)
    auth.clock = lambda: sessions[0]["expires"] + 1000
    assert client.get("/api/pi/sessions").status_code == 401
    assert not seen


@pytest.mark.parametrize("failure", ["unavailable", "redirect", "malformed", "missing_owner"])
def test_upstream_failure_is_explicit_and_never_retried(tmp_path, failure):
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        if failure == "unavailable":
            raise httpx.ConnectError("down")
        if failure == "redirect":
            return httpx.Response(
                302, json={"redirect": True}, headers={"Location": "https://evil.invalid"}
            )
        return httpx.Response(200, text="not json")

    config = Config(
        ORIGIN,
        str(auth.path),
        "http://pi:8050",
        RUNTIME,
        owner_key="" if failure == "missing_owner" else OWNER,
    )
    with TestClient(
        create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=ORIGIN
    ) as client:
        headers = sign_in(client)
        response = client.post(
            "/api/owner/requests/r/decision",
            json={"status": "approved"},
            headers=verified_headers(
                client, headers, "/api/owner/requests/r/decision", {"status": "approved"}
            ),
        )
        assert response.status_code in {502, 503}
        assert len(seen) == (0 if failure == "missing_owner" else 1)


def test_pi_runtime_key_has_no_owner_authority_or_future_admin_access(monkeypatch, tmp_path):
    from pi import api

    monkeypatch.setenv("PI_ADMIN_KEY", "recovery-only-key-" + "a" * 32)
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "pi.db"))
    monkeypatch.setenv("PI_GATEWAY_KEY_SHA256", hashlib.sha256(RUNTIME.encode()).hexdigest())
    with TestClient(api.app) as client:
        assert (
            client.post("/sessions", json={}, headers={"X-Pi-Gateway-Key": RUNTIME}).status_code
            == 200
        )
        assert client.post("/sessions", json={}, headers={"X-Pi-Key": RUNTIME}).status_code == 401
        assert (
            client.post("/sessions", json={}, headers={"X-Pi-Gateway-Key": OWNER}).status_code
            == 401
        )
        from starlette.requests import Request

        with pytest.raises(Exception) as error:
            api.require_key(
                Request({"type": "http", "method": "POST", "path": "/admin/reset", "headers": []}),
                x_pi_key=None,
                gateway_key=RUNTIME,
            )
        assert error.value.status_code == 403


def test_health_is_honest_when_dependencies_are_unavailable(gateway):
    client, _, _ = gateway
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert "runtime" in health.json()["degraded"]
    assert "csrf" not in health.text and OWNER not in health.text


def test_owner_detail_and_bounded_list_pagination_keep_dedicated_authority(gateway):
    client, _, seen = gateway
    assert client.get("/api/owner/requests/request_1").status_code == 401
    sign_in(client)
    assert client.get("/api/owner/requests?limit=1&cursor=request_1").status_code == 200
    assert (
        str(seen[-1].url) == "http://toolgate-api:8010/v2/owner/requests?limit=1&cursor=request_1"
    )
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert "X-ToolGate-Key" not in seen[-1].headers
    assert client.get("/api/owner/requests/request_1").status_code == 200
    assert str(seen[-1].url) == "http://toolgate-api:8010/v2/owner/requests/request_1"
    before = len(seen)
    for path in (
        "/api/owner/requests?limit=201",
        "/api/owner/requests?limit=0",
        "/api/owner/requests?limit=1&limit=2",
        "/api/owner/requests?secret=hidden",
        "/api/owner/requests?cursor=bad%2Fid",
        "/api/owner/requests/request_1?limit=1",
    ):
        assert client.get(path).status_code == 422
    assert len(seen) == before


def test_task_ledger_routes_use_runtime_authority_and_keep_csrf_boundary(gateway):
    client, _, seen = gateway
    assert client.get("/api/pi/tasks").status_code == 401
    headers = sign_in(client)
    for path in (
        "/tasks",
        "/tasks/task_one/update",
        "/tasks/task_one/transition",
        "/tasks/task_one/archive",
    ):
        before = len(seen)
        assert client.post("/api/pi" + path, json={}).status_code == 403
        assert len(seen) == before
        assert (
            client.post(
                "/api/pi" + path,
                json={},
                headers=verified_headers(client, headers, "/api/pi" + path, {}),
            ).status_code
            == 200
        )
        assert seen[-1].headers["X-Pi-Gateway-Key"] == RUNTIME
        assert "X-ToolGate-Owner-Key" not in seen[-1].headers
    for path in (
        "/tasks",
        "/tasks/task_one",
        "/tasks/requests/request_identity_0001",
        "/runs",
        "/runs/turn_one",
        "/events",
    ):
        assert client.get("/api/pi" + path).status_code == 200
    before = len(seen)
    for path in ("/tasks/task_one/run", "/events", "/runs/turn_one/cancel"):
        assert client.post("/api/pi" + path, json={}, headers=headers).status_code == 403
    assert len(seen) == before


def stream_gateway(tmp_path, upstream):
    auth = AuthStore(tmp_path / "stream-auth.db")
    auth.set_password(PASSWORD)
    config = Config(ORIGIN, str(auth.path), "http://pi:8050", RUNTIME, owner_key=OWNER)
    return TestClient(
        create_app(config, store=auth, stream_transport=httpx.MockTransport(upstream)),
        base_url=ORIGIN,
    )


def test_live_preview_is_relayed_without_upstream_headers(tmp_path):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=b'id: 1\nevent: delta\ndata: {"text": "Hi", "seq": 1}\n\n',
            headers={"content-type": "text/event-stream", "Set-Cookie": "service_secret=bad"},
        )

    with stream_gateway(tmp_path, upstream) as client:
        path = "/api/pi/turn-submissions/req_1/stream"
        assert client.get(path).status_code == 401
        assert not seen
        sign_in(client)
        response = client.get(path + "?after=3")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"text": "Hi"' in response.text
        assert "service_secret" not in response.headers.get("set-cookie", "")
        assert seen[0].url == "http://pi:8050/turn-submissions/req_1/stream?after=3"
        assert seen[0].headers["X-Pi-Gateway-Key"] == RUNTIME
        assert "cookie" not in seen[0].headers
        assert client.get("/api/pi/turn-submissions/bad.id/stream").status_code in (404, 422)
        assert client.get(path + "?after=-1").status_code == 422


@pytest.mark.parametrize(
    "reply",
    [
        lambda: httpx.Response(401, json={"detail": "secret"}),
        lambda: httpx.Response(200, json={"not": "a stream"}),
    ],
)
def test_live_preview_rejects_non_stream_upstream(tmp_path, reply):
    with stream_gateway(tmp_path, lambda request: reply()) as client:
        sign_in(client)
        response = client.get("/api/pi/turn-submissions/req_1/stream")
        assert response.status_code == 502
        assert "secret" not in response.text


def test_live_preview_upstream_outage_is_503(tmp_path):
    def upstream(request):
        raise httpx.ConnectError("sensitive")

    with stream_gateway(tmp_path, upstream) as client:
        sign_in(client)
        response = client.get("/api/pi/turn-submissions/req_1/stream")
        assert response.status_code == 503 and "sensitive" not in response.text
