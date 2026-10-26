"""Owner access routes: proof-bound writes, the chat's own key as the delegate, strict bodies."""

import json

import pytest
from test_gateway_mcp import EXECUTION_KEY, OWNER_KEY, boundary, proof  # noqa: F401 - fixture

WRITES = [
    ("/api/owner/access/delegate", {"enabled": True}),
    ("/api/owner/tools/research.search/sensitive", {"sensitive": True}),
]


@pytest.mark.parametrize("path,body", WRITES)
def test_writes_need_the_password_proof_and_forward_exactly(boundary, path, body):
    _, client, headers, seen, _ = boundary
    assert client.post(path, json=body, headers=headers).status_code == 428
    assert not seen
    response = client.post(path, json=body, headers=proof(client, headers, path, body))
    assert response.status_code == 200, response.text
    [request] = seen
    assert request.url == "http://toolgate.test" + path.replace("/api/owner/", "/v2/owner/")
    assert json.loads(request.content) == body
    assert request.headers["x-toolgate-owner-key"] == OWNER_KEY
    # Only the delegate switch names a key, and it is always the chat's own scoped key.
    assert (request.headers.get("x-toolgate-execution-key") == EXECUTION_KEY) == path.endswith(
        "/delegate"
    )


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/owner/access/delegate", {"enabled": "yes"}),
        ("/api/owner/access/delegate", {"enabled": True, "key": "other"}),
        ("/api/owner/tools/research.search/sensitive", {"enabled": True}),
        ("/api/owner/tools/bad id!/sensitive", {"sensitive": True}),
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
