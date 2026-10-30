"""Adopting a job Conker made: the owner control relay with the password bound to the body."""

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.api.browser_contract import owner_allowed, runtime_allowed, session_only_write

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic job adoption owner phrase"
OWNER = "job-adopt-owner-key-" + "o" * 32
JOB = "job_" + "c" * 32
PATH = f"/api/control/pi/jobs/{JOB}/adopt"
BODY = {"expected_revision": 2}


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"id": JOB, "createdBy": "owner", "revision": 3})

    config = Config(ORIGIN, str(auth.path), "http://pi.test", "r" * 32, pi_owner_key=OWNER)
    app = create_app(config, store=auth, transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        yield client, {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}, seen


def proof(client, headers, path, body):
    response = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": body}},
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Conker-Verification": response.json()["verification_token"]}


def test_adopting_a_job_needs_the_password_for_the_exact_revision(control):
    client, headers, seen = control
    assert client.post(PATH, json=BODY).status_code == 403  # no CSRF
    assert client.post(PATH, json=BODY, headers=headers).status_code == 428
    # A proof for another revision, or for another job, does not admit this adoption.
    stale = proof(client, headers, PATH, {"expected_revision": 1})
    assert client.post(PATH, json=BODY, headers=stale).status_code == 428
    other = proof(client, headers, f"/api/control/pi/jobs/job_{'d' * 32}/adopt", BODY)
    assert client.post(PATH, json=BODY, headers=other).status_code == 428
    assert not seen
    checked = proof(client, headers, PATH, BODY)
    response = client.post(PATH, json=BODY, headers=checked)
    assert response.status_code == 200 and response.json()["createdBy"] == "owner"
    [request] = seen
    assert str(request.url) == f"http://pi.test/jobs/{JOB}/adopt"
    assert json.loads(request.content) == BODY
    assert request.headers["x-pi-owner-key"] == OWNER
    assert "x-conker-verification" not in request.headers and "cookie" not in request.headers
    assert client.post(PATH, json=BODY, headers=checked).status_code == 428
    assert client.post(PATH + "?x=1", json=BODY, headers=checked).status_code == 422
    assert len(seen) == 1


def test_adoption_is_an_owner_write_that_keeps_the_password():
    assert owner_allowed("POST", f"/jobs/{JOB}/adopt")
    assert not session_only_write("POST", f"/jobs/{JOB}/adopt")
    assert not runtime_allowed("POST", f"/jobs/{JOB}/adopt")
    for method, path in (
        ("GET", f"/jobs/{JOB}/adopt"),
        ("POST", "/jobs/job_x/adopt"),
        ("POST", f"/jobs/{JOB}/adopt/extra"),
    ):
        assert not owner_allowed(method, path)
