"""Server actions through the host helper: reads with the session, actions with the password."""

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import host_ops
from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic host action owner phrase"
OP_ID = "ui-restart-0123456789abcdef"
SNAPSHOT = "snapshot-20261030T120000Z-0a1b2c3d"


def receipt(identity=OP_ID, operation="services.restart", arguments=None, outcome="working"):
    return {
        "id": identity,
        "operation": operation,
        "arguments": {"service": "pi"} if arguments is None else arguments,
        "argumentsDigest": "d" * 64,
        "source": "ui",
        "startedAt": "2026-10-30T12:00:00Z",
        "endedAt": None,
        "outcome": outcome,
        "reason": None,
        "check": None,
    }


def helper(wire):
    """A stand-in for conker scripts/host_ops.py route(): answers as the helper does."""
    body = json.loads(wire.content) if wire.content else None
    if wire.method == "GET" and wire.url.path == "/host/ops":
        return httpx.Response(
            200, json={"schemaVersion": 1, "operations": [receipt()], "damagedRecords": 0}
        )
    if wire.method == "GET" and wire.url.path == f"/host/ops/{OP_ID}":
        return httpx.Response(200, json=receipt(outcome="done"))
    if wire.method == "GET":
        return httpx.Response(404, json={"error": "unknown_operation_id"})
    if wire.url.path == "/host/read":
        return httpx.Response(
            200, json={"operation": body["operation"], "result": {"services": []}}
        )
    return httpx.Response(200, json=receipt(body["id"], body["operation"], body["arguments"]))


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen, reply = [], {"answer": helper}

    def transport(wire):
        seen.append(wire)
        assert wire.url.host == "conker-host"
        assert "cookie" not in wire.headers and "x-conker-verification" not in wire.headers
        return reply["answer"](wire)

    config = Config(
        ORIGIN, str(auth.path), "http://pi", "r" * 32, provider_control_socket="/private/c.sock"
    )
    app = create_app(config, store=auth, provider_transport=httpx.MockTransport(transport))
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        yield client, headers, seen, reply


def proof(client, headers, body):
    response = client.post(
        "/auth/verify",
        headers=headers,
        json={
            "password": PASSWORD,
            "operation": {"method": "POST", "path": "/api/host/ops", "body": body},
        },
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Conker-Verification": response.json()["verification_token"]}


def test_receipts_are_read_with_the_session_and_passed_through_unchanged(host):
    client, _, seen, _ = host
    listing = client.get("/api/host/ops")
    assert listing.status_code == 200
    assert listing.json() == {"schemaVersion": 1, "operations": [receipt()], "damagedRecords": 0}
    one = client.get(f"/api/host/ops/{OP_ID}")
    assert one.status_code == 200 and one.json() == receipt(outcome="done")
    missing = client.get("/api/host/ops/ui-missing-0123456789abcdef")
    assert missing.status_code == 404 and missing.json() == {"error": "unknown_operation_id"}
    assert [wire.url.path for wire in seen] == [
        "/host/ops",
        f"/host/ops/{OP_ID}",
        "/host/ops/ui-missing-0123456789abcdef",
    ]
    for path in (
        "/api/host/ops?limit=5",
        "/api/host/ops/short",
        "/api/host/ops/bad id!xxxxxxxxxxxx",
    ):
        response = client.get(path)
        assert response.status_code in (404, 422)
    assert client.get("/api/host/ops?limit=5").json()["error"] == "invalid_host_operation"
    assert len(seen) == 3
    client.cookies.clear()
    assert client.get("/api/host/ops").status_code == 401
    assert len(seen) == 3


@pytest.mark.parametrize("operation", ["services.status", "backup.list"])
def test_reads_need_the_session_not_a_password(host, operation):
    client, headers, seen, _ = host
    body = {"operation": operation, "arguments": {}}
    response = client.post("/api/host/read", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"operation": operation, "result": {"services": []}}
    assert json.loads(seen[0].content) == body and seen[0].url.path == "/host/read"
    assert client.post("/api/host/read", json=body).status_code == 403  # no CSRF
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "services.restart", "arguments": {"service": "pi"}},
        {"operation": "backup.run", "arguments": {}},
        {"operation": "shell", "arguments": {}},
        {"operation": "services.status", "arguments": {"service": "pi"}},
        {"operation": "services.status"},
        {"operation": "services.status", "arguments": {}, "id": OP_ID},
        {"operation": ["services.status"], "arguments": {}},
    ],
)
def test_a_read_outside_the_allowlist_never_reaches_the_helper(host, body):
    client, headers, seen, _ = host
    response = client.post("/api/host/read", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_host_operation"
    assert not seen


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("services.restart", {"service": "pi"}),
        ("backup.verify", {"snapshot": SNAPSHOT}),
        ("backup.run", {}),
    ],
)
def test_actions_need_the_password_bound_to_the_exact_body(host, operation, arguments):
    client, headers, seen, _ = host
    body = {"id": OP_ID, "operation": operation, "arguments": arguments}
    refused = client.post("/api/host/ops", json=body, headers=headers)
    assert refused.status_code == 428 and not seen
    # A proof for another action, or the same action with another id, does not admit this one.
    other = proof(client, headers, {**body, "id": "ui-other-0123456789abcdef"})
    assert client.post("/api/host/ops", json=body, headers=other).status_code == 428
    checked = proof(client, headers, body)
    response = client.post("/api/host/ops", json=body, headers=checked)
    assert response.status_code == 200
    assert response.json() == receipt(OP_ID, operation, arguments)
    [wire] = seen
    assert wire.method == "POST" and wire.url.path == "/host/ops"
    assert json.loads(wire.content) == body
    assert client.post("/api/host/ops", json=body, headers=checked).status_code == 428
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"id": OP_ID, "operation": "services.status", "arguments": {}},
        {"id": OP_ID, "operation": "services.stop", "arguments": {"service": "pi"}},
        {"id": OP_ID, "operation": "services.restart", "arguments": {"service": "Pi; rm"}},
        {"id": OP_ID, "operation": "services.restart", "arguments": {}},
        {"id": OP_ID, "operation": "backup.verify", "arguments": {"snapshot": "../etc"}},
        {"id": OP_ID, "operation": "backup.run", "arguments": {"force": "yes"}},
        {"id": "short", "operation": "backup.run", "arguments": {}},
        {"operation": "backup.run", "arguments": {}},
        {"id": OP_ID, "operation": "backup.run", "arguments": {}, "source": "cli"},
    ],
)
def test_an_action_outside_the_allowlist_is_refused_before_any_proof(host, body):
    client, headers, seen, _ = host
    response = client.post("/api/host/ops", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_host_operation"
    assert not seen


def test_helper_refusals_pass_through_and_odd_answers_do_not(host):
    client, headers, _, reply = host
    body = {"operation": "services.status", "arguments": {}}
    busy = {"error": "host_busy", "message": "Another server action is running."}
    reply["answer"] = lambda wire: httpx.Response(409, json=busy)
    response = client.post("/api/host/read", json=body, headers=headers)
    assert response.status_code == 409 and response.json() == busy
    for answer in (
        lambda wire: httpx.Response(500, json={"error": "x"}),
        lambda wire: httpx.Response(409, json={"error": "x", "detail": "secret"}),
        lambda wire: httpx.Response(200, json={"operation": "backup.list", "result": {}}),
        lambda wire: httpx.Response(200, text="upstream-private-secret"),
        lambda wire: httpx.Response(
            200, json={"operation": "services.status", "result": "x" * 300000}
        ),
        lambda wire: httpx.Response(307, headers={"location": "http://elsewhere.test"}),
    ):
        reply["answer"] = answer
        response = client.post("/api/host/read", json=body, headers=headers)
        assert response.status_code == 503
        assert response.json()["error"] == "host_unavailable"
        assert "secret" not in response.text
    # A receipt for another action is not the answer to this one.
    action = {"id": OP_ID, "operation": "backup.run", "arguments": {}}
    reply["answer"] = lambda wire: httpx.Response(200, json=receipt(operation="services.restart"))
    response = client.post("/api/host/ops", json=action, headers=proof(client, headers, action))
    assert response.status_code == 503 and response.json()["error"] == "host_unavailable"


def test_without_a_helper_the_screen_reads_the_helpers_refusal_shape(tmp_path):
    auth = AuthStore(tmp_path / "auth.db")
    config = Config(ORIGIN, str(auth.path), "http://pi", "r" * 32)
    with TestClient(create_app(config, store=auth), base_url=ORIGIN) as client:
        assert client.get("/api/host/ops").status_code == 401
    with pytest.raises(host_ops.HostOpsError) as refused:
        host_ops.request(None, "", "GET", "/host/ops")
    assert (refused.value.status, refused.value.code) == (503, "host_unavailable")
