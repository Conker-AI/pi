"""Owner runs of any tool by hand: a run asks for the password, its history needs the session."""

import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_gateway_mcp import (  # noqa: F401 - fixture
    EXECUTION_KEY,
    ORIGIN,
    OWNER_KEY,
    PASSWORD,
    boundary,
    proof,
)

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore

PATH = "/api/owner/tools/sandbox.run/runs"
RUN = {"action_id": "owner_" + "a" * 32, "args": {"command": "ls"}}


def test_a_run_needs_the_password_for_its_exact_body_and_uses_the_chats_key(boundary):
    _, client, headers, seen, _ = boundary
    assert client.post(PATH, json=RUN).status_code == 403  # no CSRF
    assert client.post(PATH, json=RUN, headers=headers).status_code == 428
    # A proof for other arguments, or for another tool, does not admit this run.
    other = proof(client, headers, PATH, {**RUN, "args": {"command": "rm -rf /"}})
    assert client.post(PATH, json=RUN, headers=other).status_code == 428
    elsewhere = proof(client, headers, "/api/owner/tools/web.search/runs", RUN)
    assert client.post(PATH, json=RUN, headers=elsewhere).status_code == 428
    assert not seen
    checked = proof(client, headers, PATH, RUN)
    response = client.post(PATH, json=RUN, headers=checked)
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert "set-cookie" not in response.headers
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == "http://toolgate.test/v2/owner/tools/sandbox.run/runs"
    assert json.loads(request.content) == RUN
    assert request.headers["x-toolgate-owner-key"] == OWNER_KEY
    assert request.headers["x-toolgate-execution-key"] == EXECUTION_KEY
    assert "x-conker-verification" not in request.headers and "cookie" not in request.headers
    assert client.post(PATH, json=RUN, headers=checked).status_code == 428
    assert len(seen) == 1


def test_resuming_an_asked_run_names_its_approval_request(boundary):
    _, client, headers, seen, _ = boundary
    resume = {**RUN, "approval_request_id": "req_0123456789"}
    response = client.post(PATH, json=resume, headers=proof(client, headers, PATH, resume))
    assert response.status_code == 200
    assert json.loads(seen[0].content) == resume


@pytest.mark.parametrize(
    "path,body",
    [
        (PATH, {"args": {}}),
        (PATH, {"action_id": "owner_" + "a" * 32}),
        (PATH, {**RUN, "action_id": "editor_" + "a" * 32}),
        (PATH, {**RUN, "action_id": "owner_" + "A" * 32}),
        (PATH, {**RUN, "args": ["ls"]}),
        (PATH, {**RUN, "approval_request_id": "x" * 101}),
        (PATH, {**RUN, "approval_request_id": 7}),
        (PATH, {**RUN, "agent": "conker"}),
        (PATH + "?retry=1", RUN),
        ("/api/owner/tools/bad id!/runs", RUN),
    ],
)
def test_a_run_outside_the_contract_never_reaches_toolgate(boundary, path, body):
    _, client, headers, seen, _ = boundary
    assert client.post(path, json=body, headers=headers).status_code in (404, 422)
    assert not seen


def test_run_history_is_a_session_read_with_the_owner_key_alone(boundary):
    _, client, _, seen, _ = boundary
    response = client.get(PATH + "?limit=10", headers={"X-ToolGate-Execution-Key": "browser"})
    assert response.status_code == 200 and response.json() == {"ok": True}
    [request] = seen
    assert str(request.url) == "http://toolgate.test/v2/owner/tools/sandbox.run/runs?limit=10"
    assert request.headers["x-toolgate-owner-key"] == OWNER_KEY
    assert "x-toolgate-execution-key" not in request.headers
    assert client.get(PATH).status_code == 200
    for query in ("?limit=0", "?limit=51", "?limit=x", "?limit=1&limit=2", "?cursor=a"):
        assert client.get(PATH + query).status_code == 422, query
    assert len(seen) == 2
    client.cookies.clear()
    assert client.get(PATH).status_code == 401
    assert len(seen) == 2


def test_toolgate_answers_pass_through_and_failures_are_explicit(boundary):
    app, client, headers, seen, mode = boundary
    mode["response"] = "timeout"
    response = client.post(PATH, json=RUN, headers=proof(client, headers, PATH, RUN))
    assert response.status_code == 503 and "secret" not in response.text
    mode["response"] = "redirect"
    assert client.get(PATH).status_code == 502
    app.state.config = replace(app.state.config, toolgate_execution_key="")
    mode["response"] = "ok"
    unconfigured = client.post(PATH, json=RUN, headers=proof(client, headers, PATH, RUN))
    assert unconfigured.status_code == 503
    assert len(seen) == 2


def test_a_confirmation_required_answer_reaches_the_owner_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    asked = {
        "detail": {
            "code": "CONFIRMATION_REQUIRED",
            "message": "This tool asks first.",
            "approval_request_id": "req_0123456789",
        }
    }
    config = Config(
        ORIGIN,
        str(auth.path),
        "http://pi.test",
        "r" * 32,
        toolgate_url="http://toolgate.test",
        owner_key=OWNER_KEY,
        toolgate_execution_key=EXECUTION_KEY,
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json=asked))
    with TestClient(create_app(config, store=auth, transport=transport), base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        response = client.post(PATH, json=RUN, headers=proof(client, headers, PATH, RUN))
        assert response.status_code == 409 and response.json() == asked
