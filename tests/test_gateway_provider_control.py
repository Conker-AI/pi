import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.provider_control import ProviderControlError, request, validate_operation
from gateway.store import AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic owner verification password"
REVISION = "credential_" + "a" * 32


def status():
    return {
        "schemaVersion": 1,
        "available": True,
        "secretsIncluded": False,
        "paidAllowed": False,
        "policyRecoveryRequired": False,
        "providers": [
            {
                "id": provider,
                "configured": False,
                "activeRevision": None,
                "activeAt": None,
                "stagedRevision": None,
                "stagedAt": None,
                "verificationStatus": None,
                "verificationBasis": None,
                "verifiedAt": None,
                "verificationStale": False,
                "activationPending": False,
                "revokedRevisions": [],
                "secretIncluded": False,
            }
            for provider in ("openrouter", "openai", "anthropic")
        ],
    }


def test_provider_mutation_has_same_origin_password_and_single_use_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def host(wire):
        seen.append(wire)
        assert wire.url.host == "conker-host" and wire.url.path == "/providers"
        assert "cookie" not in wire.headers and "x-pi-owner-key" not in wire.headers
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
        path = "/api/host/providers"
        assert client.get(path).status_code == 401
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        assert client.get(path).json() == status()
        body = {
            "provider": "openai",
            "operation": "stage",
            "secret": "synthetic-provider-key",
            "activeRevision": None,
            "stagedRevision": None,
        }
        assert client.post(path, json=body, headers=headers).status_code == 428
        assert (
            client.post(
                path, json=body, headers={**headers, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        proof = client.post(
            "/auth/verify",
            json={
                "password": PASSWORD,
                "operation": {"method": "POST", "path": path, "body": body},
            },
            headers=headers,
        )
        checked = {**headers, "X-Conker-Verification": proof.json()["verification_token"]}
        assert (
            client.post(
                path, json={**body, "secret": "another-synthetic-key"}, headers=checked
            ).status_code
            == 428
        )
        saved = client.post(path, json=body, headers=checked)
        assert saved.status_code == 200
        assert "synthetic-provider-key" not in saved.text
        assert client.post(path, json=body, headers=checked).status_code == 428
        assert len(seen) == 2
        assert client.get(path + "?url=https://evil.test").status_code == 422
        assert (
            client.post(
                path, json={"operation": "execute", "command": "whoami"}, headers=headers
            ).status_code
            == 422
        )
        assert b"synthetic-provider-key" not in auth.path.read_bytes()


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "activate", "provider": "openai", "revision": "--option"},
        {
            "operation": "stage",
            "provider": "openai",
            "secret": "private\nkey",
            "activeRevision": None,
            "stagedRevision": None,
        },
        {"operation": "verify", "provider": "custom", "revision": REVISION},
        {
            "operation": "verify",
            "provider": "openai",
            "revision": REVISION,
            "url": "https://evil.test",
        },
        {
            "operation": "record-revoked",
            "provider": "openai",
            "revision": REVISION,
            "issuerConfirmed": False,
        },
        {"operation": []},
    ],
)
def test_rejected_input_is_not_echoed(body):
    with pytest.raises(ProviderControlError) as error:
        validate_operation(body)
    assert str(error.value) == "Invalid provider operation."


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://evil.test"}),
        httpx.Response(400, json={"secret": "private"}),
        httpx.Response(200, json={**status(), "secret": "private"}),
        httpx.Response(200, json={**status(), "providers": []}),
        httpx.Response(200, content=b"x" * 65537, headers={"Content-Type": "application/json"}),
    ],
)
def test_redirect_errors_secrets_and_invalid_status_are_not_forwarded(response):
    with pytest.raises(ProviderControlError) as error:
        request("/private/control.sock", "GET", transport=httpx.MockTransport(lambda _: response))
    assert "private" not in str(error.value)


def test_unconfigured_bridge_is_unavailable_not_fake_connected():
    assert request("", "GET")["available"] is False
    with pytest.raises(ProviderControlError):
        request("", "POST", {"operation": "verify", "provider": "openai", "revision": REVISION})
