import base64

from gateway.terminals import Terminals
from tests.test_gateway_verification import boundary, proof  # noqa: F401
from tests.test_terminal_leases import FakeTerminal


def test_terminal_requires_password_proof_csrf_and_live_owner_session(request):
    client, _auth, headers, _seen, now = request.getfixturevalue("boundary")
    spawned = []

    def factory(*args):
        terminal = FakeTerminal()
        spawned.append(terminal)
        return terminal

    client.app.state.terminals = Terminals("/bin/bash", "/synthetic", factory=factory)
    body = {"requestId": "terminal_request_01"}
    assert client.post("/api/terminal", json=body).status_code == 403
    assert client.post("/api/terminal", json=body, headers=headers).status_code == 428
    assert not spawned
    verified = proof(client, headers, "/api/terminal", body)
    admitted = {**headers, "X-Conker-Verification": verified["verification_token"]}
    assert client.post("/api/terminal", json=body, headers=admitted).status_code == 200
    assert client.post("/api/terminal", json=body, headers=headers).json()["replayed"]
    assert len(spawned) == 1
    path = "/api/terminal/terminal_request_01/input"
    data = {"data": base64.b64encode(b"synthetic input").decode()}
    assert client.post(path, json=data).status_code == 403
    response = client.post(path, json=data, headers=headers)
    assert response.status_code == 200 and response.json()["acceptedBytes"] == 15
    assert response.headers["cache-control"] == "no-store"
    assert spawned[0].writes == [b"synthetic input"]
    now[0] += 61
    client.app.state.terminals.sweep()
    assert spawned[0].closed
    assert client.post(path, json=data, headers=headers).status_code == 401


def test_terminal_is_disabled_without_operator_config(request):
    client, _auth, headers, _seen, _now = request.getfixturevalue("boundary")
    assert (
        client.post(
            "/api/terminal", json={"requestId": "terminal_request_01"}, headers=headers
        ).status_code
        == 503
    )
