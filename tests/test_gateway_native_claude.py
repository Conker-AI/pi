import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore


def test_native_auth_needs_the_password_only_for_api_billing(tmp_path, monkeypatch):
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
        loginId=None,
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
        # Signing in with the plan spends nothing per call: the session is enough.
        body = {"operation": "login", "billing": "subscription"}
        assert client.post(path, json=body).status_code == 403
        assert (
            client.post(path, json={**body, "token": "not-accepted"}, headers=headers).status_code
            == 422
        )
        assert client.get(path + "?secret=x").status_code == 422
        assert len(seen) == 1  # only the earlier read
        assert client.post(path, json=body, headers=headers).status_code == 200
        assert json.loads(seen[-1].content) == body
        assert "x-conker-verification" not in seen[-1].headers
        assert len(seen) == 2

        # Signing in with API billing spends money per call: it needs the exact one-use proof.
        body = {"operation": "login", "billing": "api"}
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
        assert client.post(path, json={**body, "x": 1}, headers=checked).status_code == 422
        assert len(seen) == 2
        assert client.post(path, json=body, headers=checked).status_code == 200
        assert json.loads(seen[-1].content) == body
        assert "x-conker-verification" not in seen[-1].headers
        assert client.post(path, json=body, headers=checked).status_code == 428
        assert len(seen) == 3

        login_id = "login_" + "a" * 32
        replacement_id = "login_" + "b" * 32
        body = {"operation": "cancel", "loginId": login_id}
        for invalid in (
            {"operation": "cancel"},
            {"operation": "cancel", "loginId": None},
            {**body, "billing": "subscription"},
            {**body, "loginId": "invalid"},
            {"operation": "login", "loginId": login_id},
        ):
            assert client.post(path, json=invalid, headers=headers).status_code == 422
        assert len(seen) == 3
        # Cancelling an attempt only stops something: the session is enough, no proof.
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json=body, headers=headers).status_code == 200
        assert json.loads(seen[-1].content) == body
        assert "x-conker-verification" not in seen[-1].headers
        assert len(seen) == 4
        assert (
            client.post(path, json={**body, "loginId": replacement_id}, headers=headers).status_code
            == 200
        )
        assert json.loads(seen[-1].content) == {**body, "loginId": replacement_id}
        assert len(seen) == 5


def test_current_native_claude_authorization_domain_is_preserved():
    from gateway.native_claude import request

    value = dict(
        available=True,
        connected=False,
        loginState="pending",
        loginId="login_" + "a" * 32,
        authorizationUrl="https://claude.com/cai/oauth/authorize?state=synthetic",
        credentialsIncluded=False,
        runtime="Claude Code",
        models=[],
    )
    assert request(httpx.MockTransport(lambda _: httpx.Response(200, json=value)), "GET") == value


@pytest.mark.parametrize(
    ("state", "identity"),
    [("pending", None), ("pending", "invalid"), ("canceled", "login_" + "a" * 32)],
)
def test_native_status_rejects_ambiguous_attempt_identity(state, identity):
    from gateway.native_claude import request
    from gateway.provider_control import ProviderControlError

    value = dict(
        available=True,
        connected=False,
        loginState=state,
        loginId=identity,
        authorizationUrl=None,
        credentialsIncluded=False,
        runtime="Claude Code",
        models=[],
    )
    with pytest.raises(ProviderControlError):
        request(httpx.MockTransport(lambda _: httpx.Response(200, json=value)), "GET")
    assert request(None, "GET")["loginId"] is None
