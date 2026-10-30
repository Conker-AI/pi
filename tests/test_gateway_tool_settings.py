"""ToolGate's owner settings and lockdown, and Pi's model usage, through the browser gateway."""

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic tool settings owner phrase"
OWNER_KEY = "toolgate-owner-" + "o" * 32
PI_OWNER = "pi-owner-control-" + "p" * 32
SETTINGS = {"expected_revision": 3, "settings": {"tool_seconds": 60, "web_region": "il"}}


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen, reply = [], {"answer": lambda request: httpx.Response(200, json={"revision": 4})}

    def upstream(request):
        seen.append(request)
        return reply["answer"](request)

    config = Config(
        ORIGIN,
        str(auth.path),
        "http://pi.test",
        "r" * 32,
        toolgate_url="http://toolgate.test",
        owner_key=OWNER_KEY,
        pi_owner_key=PI_OWNER,
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
        yield client, headers, seen, reply


def proof(client, headers, method, path, body):
    response = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": method, "path": path, "body": body}},
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Conker-Verification": response.json()["verification_token"]}


def test_tool_settings_are_read_with_the_session_through_the_owner_key(gate):
    client, _, seen, _ = gate
    response = client.get("/api/owner/tool-settings", headers={"X-ToolGate-Owner-Key": "browser"})
    assert response.status_code == 200 and response.json() == {"revision": 4}
    [request] = seen
    assert str(request.url) == "http://toolgate.test/v2/owner/settings"
    assert request.method == "GET" and request.headers["x-toolgate-owner-key"] == OWNER_KEY
    assert "cookie" not in request.headers and "x-toolgate-execution-key" not in request.headers
    assert client.get("/api/owner/tool-settings?x=1").status_code == 422
    client.cookies.clear()
    assert client.get("/api/owner/tool-settings").status_code == 401
    assert len(seen) == 1


def test_saving_tool_settings_needs_the_password_for_the_exact_document(gate):
    client, headers, seen, _ = gate
    path = "/api/owner/tool-settings"
    assert client.put(path, json=SETTINGS, headers=headers).status_code == 428
    # No proof is issued for it as a POST, and one for other settings does not admit this save.
    as_post = client.post(
        "/auth/verify",
        headers=headers,
        json={
            "password": PASSWORD,
            "operation": {"method": "POST", "path": path, "body": SETTINGS},
        },
    )
    assert as_post.status_code == 422
    other = proof(client, headers, "PUT", path, {**SETTINGS, "expected_revision": 4})
    assert client.put(path, json=SETTINGS, headers=other).status_code == 428
    assert not seen
    checked = proof(client, headers, "PUT", path, SETTINGS)
    response = client.put(path, json=SETTINGS, headers=checked)
    assert response.status_code == 200
    [request] = seen
    assert request.method == "PUT" and request.url.path == "/v2/owner/settings"
    assert json.loads(request.content) == SETTINGS
    assert "x-conker-verification" not in request.headers
    assert client.put(path, json=SETTINGS, headers=checked).status_code == 428
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"settings": {}},
        {"expected_revision": "3", "settings": {}},
        {"expected_revision": -1, "settings": {}},
        {"expected_revision": True, "settings": {}},
        {"expected_revision": 3, "settings": []},
        {"expected_revision": 3, "settings": {}, "lockdown": True},
    ],
)
def test_a_tool_settings_save_has_exactly_its_two_fields(gate, body):
    client, headers, seen, _ = gate
    assert client.put("/api/owner/tool-settings", json=body, headers=headers).status_code == 422
    assert not seen


def test_toolgates_refusals_pass_through(gate):
    client, headers, _, reply = gate
    stale = {"detail": {"code": "stale_revision", "message": "Reload the settings."}}
    reply["answer"] = lambda request: httpx.Response(409, json=stale)
    path = "/api/owner/tool-settings"
    response = client.put(
        path, json=SETTINGS, headers=proof(client, headers, "PUT", path, SETTINGS)
    )
    assert response.status_code == 409 and response.json() == stale


def test_lockdown_turns_on_with_the_session_and_off_only_with_the_password(gate):
    client, headers, seen, reply = gate
    view = {"lockdown": True, "reason": "", "changed_by": "owner"}
    reply["answer"] = lambda request: httpx.Response(200, json=view)
    path = "/api/owner/lockdown"
    assert client.get(path).json() == view
    assert (
        seen[-1].method == "GET" and str(seen[-1].url) == "http://toolgate.test/v2/owner/lockdown"
    )
    on = client.post(path, json={"enabled": True}, headers=headers)
    assert on.status_code == 200 and on.json() == view
    assert json.loads(seen[-1].content) == {"enabled": True}
    assert seen[-1].headers["x-toolgate-owner-key"] == OWNER_KEY
    assert client.post(path, json={"enabled": True}).status_code == 403  # no CSRF
    off = {"enabled": False}
    assert client.post(path, json=off, headers=headers).status_code == 428
    wrong = proof(client, headers, "POST", path, {"enabled": True})
    assert client.post(path, json=off, headers=wrong).status_code == 428
    assert len(seen) == 2
    checked = proof(client, headers, "POST", path, off)
    assert client.post(path, json=off, headers=checked).status_code == 200
    assert json.loads(seen[-1].content) == off
    assert client.post(path, json=off, headers=checked).status_code == 428
    assert len(seen) == 3
    for body in ({}, {"enabled": "false"}, {"enabled": 0}, {"enabled": False, "reason": "x"}):
        assert client.post(path, json=body, headers=headers).status_code == 422
    assert (
        client.post(path + "?enabled=true", json={"enabled": True}, headers=headers).status_code
        == 422
    )
    assert len(seen) == 3
    client.cookies.clear()
    assert client.get(path).status_code == 401
    assert len(seen) == 3


def test_without_the_owner_key_toolgate_routes_are_503(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    config = Config(ORIGIN, str(auth.path), "http://pi.test", "r" * 32)
    with TestClient(create_app(config, store=auth), base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert client.get("/api/owner/tool-settings").status_code == 503
        assert client.get("/api/owner/lockdown").status_code == 503


def test_model_usage_is_a_session_read_of_pi(gate):
    client, headers, seen, reply = gate
    usage = {"calls": [], "totals": []}
    reply["answer"] = lambda request: httpx.Response(200, json=usage)
    response = client.get("/api/control/pi/inference/usage?limit=20")
    assert response.status_code == 200 and response.json() == usage
    [request] = seen
    assert str(request.url) == "http://pi.test/inference/usage?limit=20"
    assert request.headers["x-pi-owner-key"] == PI_OWNER
    assert "x-pi-gateway-key" not in request.headers and "cookie" not in request.headers
    refusal = {"detail": "Request validation failed."}
    reply["answer"] = lambda request: httpx.Response(422, json=refusal)
    assert client.get("/api/control/pi/inference/usage?limit=999").json() == refusal
    # Read-only: the browser cannot write to it.
    assert (
        client.post("/api/control/pi/inference/usage", json={}, headers=headers).status_code == 403
    )
    assert client.get("/api/pi/inference/usage").status_code == 403
    assert len(seen) == 2
    client.cookies.clear()
    assert client.get("/api/control/pi/inference/usage").status_code == 401
    assert len(seen) == 2


def test_the_password_table_names_exactly_the_new_writes():
    from gateway.operations import fingerprint
    from gateway.store import AuthError

    for method, path in (
        ("PUT", "/api/owner/tool-settings"),
        ("POST", "/api/owner/lockdown"),
        ("POST", "/api/host/ops"),
        ("POST", "/api/control/pi/imports/claude/" + "a" * 64),
    ):
        assert len(fingerprint(method, path, {})) == 64
    for method, path in (
        ("POST", "/api/owner/tool-settings"),
        ("PUT", "/api/owner/lockdown"),
        ("PUT", "/api/control/pi/owner/settings"),
        ("POST", "/api/host/read"),
        ("GET", "/api/host/ops"),
        ("DELETE", "/api/host/ops"),
        ("POST", "/api/control/pi/imports/gemini/" + "a" * 64),
    ):
        with pytest.raises(AuthError):
            fingerprint(method, path, {})
