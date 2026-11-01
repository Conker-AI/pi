"""Pi's side of the v1 chat routes: the owner key reaches exactly these, for a well-formed id only.

Every operation that browser_contract.CHAT_READS / CHAT_WRITES admits is called here with each
credential: the owner key and the recovery key get past authorization, the runtime key and no key
are refused, a malformed id is refused for the owner, and an unknown body field is refused by Pi's
own strict model. The chat in each request does not exist, so nothing is written; the gateway's
half (session, CSRF, proof) is tests/test_gateway_chat_routes.py.
"""

import hashlib
import re
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.api.browser_contract import CHAT_READS, CHAT_WRITES, owner_allowed
from pi.conversation.store import Store
from tests.test_gateway_chat_routes import CHAT as GATEWAY_CHAT
from tests.test_gateway_chat_routes import ITEM, MESSAGE, REQUEST, WRITES, name

GATEWAY, OWNER, ADMIN = "gateway-chat-test-key", "owner-chat-test-key", "admin-chat-test-key"
CHAT = "ses_" + "f" * 16  # well formed, never created
TYPED = {
    "session_id": CHAT,
    "sid": CHAT,
    "message_id": MESSAGE,
    "mid": MESSAGE,
    "root": MESSAGE,
    "item_id": ITEM,
    "request_id": REQUEST,
}
# The gateway test's valid body for each write, keyed by its path with the chat id left out.
BODY = {path.replace(GATEWAY_CHAT, "{chat}"): body for path, body in WRITES}


def typed(path: str) -> str:
    """The path with each parameter filled with a well-formed id of its kind."""
    sample = "sample_0123456789abcdef"
    return re.sub(r"\{([^}]+)\}", lambda match: TYPED.get(match.group(1), sample), path)


def chat_operations():
    """(method, OpenAPI path) for every Pi operation a chat pattern admits."""
    found = []
    for path, methods in api.app.openapi()["paths"].items():
        for method in methods:
            patterns = CHAT_READS if method == "get" else CHAT_WRITES
            if path.startswith(("/sessions/{", "/context/{")) and any(
                re.fullmatch(pattern, typed(path)) for pattern in patterns
            ):
                found.append((method.upper(), path))
    return found


OPERATIONS = chat_operations()


@pytest.fixture
def client(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        state = api.app.state
        monkeypatch.setattr(state, "store", store, raising=False)
        monkeypatch.setattr(state, "loop", SimpleNamespace(store=store), raising=False)
        monkeypatch.setattr(state, "router", SimpleNamespace(adapters=lambda: {}), raising=False)
        monkeypatch.setattr(state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            state, "gateway_key_hash", hashlib.sha256(GATEWAY.encode()).hexdigest(), raising=False
        )
        monkeypatch.setattr(
            state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
        )
        yield TestClient(api.app, raise_server_exceptions=False)


def test_every_chat_pattern_is_one_real_operation():
    assert len(OPERATIONS) == len(CHAT_READS) + len(CHAT_WRITES)
    for method, path in OPERATIONS:
        assert owner_allowed(method, typed(path)), (method, path)


@pytest.mark.parametrize(
    "method,path", OPERATIONS, ids=[f"{method}-{name(path)}" for method, path in OPERATIONS]
)
def test_owner_key_reaches_the_route_and_nothing_else_does(client, method, path):
    target = typed(path)
    body = BODY.get(target.replace(CHAT, "{chat}")) if method == "POST" else None
    assert method == "GET" or body is not None, path

    def call(headers, url=target, json=body):
        return client.request(method, url, headers=headers, json=json).status_code

    for headers in ({"X-Pi-Owner-Key": OWNER}, {"X-Pi-Key": ADMIN}):
        assert call(headers) not in {401, 403, 500}, (path, headers)
    assert call({"X-Pi-Gateway-Key": GATEWAY}) == 401
    assert call({"X-Pi-Owner-Key": "not-the-owner-key"}) == 401
    assert call({}) == 401
    malformed = target.replace(CHAT, "ses_" + "F" * 16)
    assert call({"X-Pi-Owner-Key": OWNER}, url=malformed) == 403
    if method == "POST":
        extra = {**body, "unexpected": True}
        assert call({"X-Pi-Owner-Key": OWNER}, json=extra) == 422
