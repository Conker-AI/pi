"""Owner access routes: widening needs a proof, narrowing the session; the chat's own key; strict bodies."""

import json

import pytest
from test_gateway_mcp import EXECUTION_KEY, OWNER_KEY, boundary, proof  # noqa: F401 - fixture

DIGEST = "a" * 64
# Widening what Conker may do (delegate on, a tool granted to chats, a tool no longer always
# asking) needs the password proof; narrowing it needs only the signed-in session.
PASSWORD_WRITES = [
    ("/api/owner/access/delegate", {"enabled": True}),
    ("/api/owner/tools/research.search/sensitive", {"sensitive": False}),
    ("/api/owner/tools/sandbox.run/access", {"enabled": True, "expected_digest": DIGEST}),
]
SESSION_WRITES = [
    ("/api/owner/access/delegate", {"enabled": False}),
    ("/api/owner/tools/research.search/sensitive", {"sensitive": True}),
    ("/api/owner/tools/sandbox.run/access", {"enabled": False, "expected_digest": DIGEST}),
]


def assert_forwarded_exactly(seen, path, body):
    [request] = seen
    assert request.url == "http://toolgate.test" + path.replace("/api/owner/", "/v2/owner/")
    assert json.loads(request.content) == body
    assert request.headers["x-toolgate-owner-key"] == OWNER_KEY
    assert "x-conker-verification" not in request.headers
    # Only the delegate switch and tool grants name a key, always the chat's own scoped key.
    assert (request.headers.get("x-toolgate-execution-key") == EXECUTION_KEY) == path.endswith(
        ("/delegate", "/access")
    )


@pytest.mark.parametrize("path,body", PASSWORD_WRITES)
def test_widening_writes_need_the_password_proof_and_forward_exactly(boundary, path, body):
    _, client, headers, seen, _ = boundary
    assert client.post(path, json=body, headers=headers).status_code == 428
    assert not seen
    response = client.post(path, json=body, headers=proof(client, headers, path, body))
    assert response.status_code == 200, response.text
    assert_forwarded_exactly(seen, path, body)


@pytest.mark.parametrize("path,body", SESSION_WRITES)
def test_narrowing_writes_need_only_the_session_and_forward_exactly(boundary, path, body):
    _, client, headers, seen, _ = boundary
    for refused in ({}, {**headers, "X-CSRF-Token": "wrong"}):
        assert client.post(path, json=body, headers=refused).status_code == 403
    assert not seen
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert_forwarded_exactly(seen, path, body)
    client.cookies.clear()
    assert client.post(path, json=body, headers=headers).status_code == 401
    assert len(seen) == 1


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/owner/access/delegate", {"enabled": "yes"}),
        ("/api/owner/access/delegate", {"enabled": True, "key": "other"}),
        ("/api/owner/tools/research.search/sensitive", {"enabled": True}),
        ("/api/owner/tools/bad id!/sensitive", {"sensitive": True}),
        ("/api/owner/tools/sandbox.run/access", {"enabled": True}),
        ("/api/owner/tools/sandbox.run/access", {"enabled": "yes", "expected_digest": DIGEST}),
        ("/api/owner/tools/sandbox.run/access", {"enabled": True, "expected_digest": "short"}),
    ],
)
def test_bodies_and_ids_are_strict(boundary, path, body):
    _, client, headers, seen, _ = boundary
    assert client.post(path, json=body, headers=headers).status_code in (404, 422)
    assert not seen


def test_reading_the_state_uses_server_owned_keys(boundary):
    _, client, _, seen, _ = boundary
    response = client.get("/api/owner/access", headers={"X-ToolGate-Execution-Key": "browser"})
    assert response.status_code == 200
    assert seen[0].headers["x-toolgate-execution-key"] == EXECUTION_KEY
    assert client.get("/api/owner/access?x=1").status_code == 422


def test_reading_a_tools_access_uses_the_chats_own_key(boundary):
    _, client, _, seen, _ = boundary
    response = client.get(
        "/api/owner/tools/sandbox.run/access", headers={"X-ToolGate-Execution-Key": "browser"}
    )
    assert response.status_code == 200
    assert seen[0].url == "http://toolgate.test/v2/owner/tools/sandbox.run/access"
    assert seen[0].headers["x-toolgate-execution-key"] == EXECUTION_KEY
