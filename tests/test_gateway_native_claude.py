import hashlib

import httpx
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore


def test_native_auth_requires_exact_owner_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    password = "synthetic test owner password"
    origin = "https://localhost:8050"
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(password)
    seen = []
    value = dict(
        available=True,
        connected=False,
        loginState="idle",
        authorizationUrl=None,
        credentialsIncluded=False,
        runtime="Claude Code",
        models=[],
    )

    def host(request):
        seen.append(request)
        return httpx.Response(200, json=value)

    app = create_app(
        Config(origin, str(auth.path), "http://pi", "r" * 32),
        store=auth,
        provider_transport=httpx.MockTransport(host),
    )
    with TestClient(app, base_url=origin) as client:
        path = "/api/host/claude"
        assert client.get(path).status_code == 401
        csrf = client.get("/auth/session").json()["csrf_token"]
        result = client.post(
            "/auth/login",
            json={"password": password},
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": origin, "X-CSRF-Token": result.json()["csrf_token"]}
        assert client.get(path).json() == value
        body = {"operation": "login", "billing": "subscription"}
        assert client.post(path, json=body, headers=headers).status_code == 428
        proof = client.post(
            "/auth/verify",
            json={
                "password": password,
                "operation": {"method": "POST", "path": path, "body": body},
            },
            headers=headers,
        ).json()["verification_token"]
        checked = {**headers, "X-Conker-Verification": proof}
        assert (
            client.post(path, json={**body, "billing": "api"}, headers=checked).status_code == 428
        )
        assert client.post(path, json=body, headers=checked).status_code == 200
        assert client.post(path, json=body, headers=checked).status_code == 428
        assert (
            client.post(path, json={**body, "token": "not-accepted"}, headers=headers).status_code
            == 422
        )
        assert client.get(path + "?secret=x").status_code == 422
        assert len(seen) == 2


def test_current_native_claude_authorization_domain_is_preserved():
    from gateway.native_claude import request

    value = dict(
        available=True,
        connected=False,
        loginState="pending",
        authorizationUrl="https://claude.com/cai/oauth/authorize?state=synthetic",
        credentialsIncluded=False,
        runtime="Claude Code",
        models=[],
    )
    assert request(httpx.MockTransport(lambda _: httpx.Response(200, json=value)), "GET") == value
