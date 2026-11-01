"""Every route refuses a credential its allowlist does not cover (browser_contract.py).

The gateway's runtime key and the owner-control key may reach only the routes runtime_allowed and
owner_allowed admit. This walks every documented operation and calls it with each key wherever the
allowlist says no; the answer must be a refusal (401 or 403). Requests that the allowlist admits
are not sent, so nothing here runs a route or writes data.
"""

import hashlib
import re
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.api.browser_contract import owner_allowed, runtime_allowed
from pi.conversation.store import Store
from tests.test_owner_chat_routes import OPERATIONS as CHAT_OPERATIONS
from tests.test_owner_chat_routes import typed

GATEWAY, OWNER, ADMIN = "gateway-runtime-test-key", "owner-control-test-key", "admin-test-key"
OPEN = {("GET", "/health")}
# Owner routes under one chat that predate the v1 chat contract; each has its own tests.
ELSEWHERE = {
    ("GET", "/context/{session_id}"),
    ("GET", "/context/{session_id}/summary"),
    ("GET", "/sessions/{identity}/settings"),
    ("POST", "/sessions/{identity}/settings"),
    ("POST", "/sessions/{session_id}/suggestions"),
}


def operations():
    for path, methods in api.app.openapi()["paths"].items():
        for method in methods:
            yield method.upper(), path


def sample(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "sample_0123456789abcdef", path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        state = api.app.state
        monkeypatch.setattr(state, "store", store, raising=False)
        monkeypatch.setattr(state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            state, "gateway_key_hash", hashlib.sha256(GATEWAY.encode()).hexdigest(), raising=False
        )
        monkeypatch.setattr(
            state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
        )
        yield TestClient(api.app, raise_server_exceptions=False)


def test_every_operation_is_listed_and_needs_a_credential(client):
    seen = list(operations())
    assert len(seen) > 250
    for method, path in seen:
        if (method, path) in OPEN:
            continue
        response = client.request(method, sample(path), json={})
        assert response.status_code in {401, 403}, (method, path, response.status_code)


@pytest.mark.parametrize(
    "header,secret,allowed",
    [
        ("X-Pi-Gateway-Key", GATEWAY, runtime_allowed),
        ("X-Pi-Owner-Key", OWNER, owner_allowed),
    ],
    ids=["gateway-runtime-key", "owner-key"],
)
def test_keys_never_reach_routes_outside_their_allowlist(client, header, secret, allowed):
    refused = 0
    for method, path in operations():
        target = sample(path)
        if (method, path) in OPEN or allowed(method, target):
            continue
        response = client.request(method, target, json={}, headers={header: secret})
        assert response.status_code in {401, 403}, (method, path, response.status_code)
        refused += 1
    assert refused > 100


def test_every_owner_route_under_a_chat_has_its_authorization_tests():
    """A chat route the owner key reaches is a CHAT_READS/CHAT_WRITES case or an older listed one.

    The chat cases are called with every credential in tests/test_owner_chat_routes.py and through
    the gateway in tests/test_gateway_chat_routes.py; a new owner route here fails until it is.
    """
    reachable = {
        (method, path)
        for method, path in operations()
        if path.startswith(("/sessions/{", "/context/{")) and owner_allowed(method, typed(path))
    }
    assert reachable == set(CHAT_OPERATIONS) | ELSEWHERE
