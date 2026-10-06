import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import chatgpt
from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.provider_control import ProviderControlError
from gateway.store import AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic owner verification password"


def status():
    return {
        "available": True,
        "connected": False,
        "connectionId": None,
        "plan": None,
        "loginId": None,
        "loginState": "idle",
        "problem": None,
        "models": [],
        "catalogueComplete": False,
        "credentialsIncluded": False,
    }


def test_subscription_auth_is_owner_only_exact_single_use_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def host(request):
        seen.append(request)
        assert request.url.path == "/chatgpt"
        assert "cookie" not in request.headers
        return httpx.Response(200, json=status())

    config = Config(
        ORIGIN,
        str(auth.path),
        "http://pi",
        "r" * 32,
        provider_control_socket="/private/control.sock",
    )
    with TestClient(
        create_app(config, store=auth, provider_transport=httpx.MockTransport(host)),
        base_url=ORIGIN,
    ) as client:
        path = "/api/host/chatgpt"
        assert client.get(path).status_code == 401
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        assert client.get(path).status_code == 200
        body = {"operation": "login"}
        assert client.post(path, json=body, headers=headers).status_code == 428
        proof = client.post(
            "/auth/verify",
            json={
                "password": PASSWORD,
                "operation": {"method": "POST", "path": path, "body": body},
            },
            headers=headers,
        ).json()["verification_token"]
        checked = {**headers, "X-Conker-Verification": proof}
        assert client.post(path, json={"operation": "models"}, headers=checked).status_code == 428
        assert (
            client.post(
                path, json=body, headers={**checked, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.post(path, json=body, headers=checked).status_code == 200
        assert client.post(path, json=body, headers=checked).status_code == 428
        assert client.get(path + "?token=secret").status_code == 422
        assert len(seen) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "login", "apiKey": "PRIVATE"},
        {"operation": "logout", "connectionId": "stale"},
        {"operation": "command/exec"},
        {"operation": []},
    ],
)
def test_bounded_auth_methods(body):
    with pytest.raises(ProviderControlError):
        chatgpt.validate_operation(body)


@pytest.mark.parametrize(
    "patch",
    [
        {"accessToken": "PRIVATE"},
        {"credentialsIncluded": True},
        {"deviceCode": {"verificationUrl": "https://evil.test", "userCode": "TEST-1234"}},
        {"connected": True},
    ],
)
def test_projection_refuses_credentials_untrusted_urls_and_impossible_states(patch):
    with pytest.raises(ProviderControlError):
        chatgpt.request(
            "/private/socket",
            "GET",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={**status(), **patch})
            ),
        )
