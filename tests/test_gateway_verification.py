"""Admission proofs, password budget and non-renewing server unlock windows."""

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as module
from gateway.api import COOKIE, Config, create_app
from gateway.operations import fingerprint
from gateway.store import AuthError, AuthStore

PASSWORD = "four quiet trees beside school"
ORIGIN = "https://localhost:8050"


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    # Existing store tests exercise real scrypt. Keep race/route matrix tests inexpensive.
    monkeypatch.setattr(
        module,
        "password_hash",
        lambda password, salt: hashlib.sha256(salt + password.encode()).hexdigest(),
    )
    now = [1000.0]
    auth = AuthStore(tmp_path / "auth.db", clock=lambda: now[0], idle_seconds=60)
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        if request.url.path == "/tasks/failure/update":
            raise httpx.ReadTimeout("private upstream detail")
        return httpx.Response(200, json={"ok": True})

    config = Config(ORIGIN, str(auth.path), "http://pi:8050", "r" * 32, owner_key="o" * 32)
    with TestClient(
        create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=ORIGIN
    ) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        response = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
        yield client, auth, headers, seen, now


def proof(client, headers, path, body):
    response = client.post(
        "/auth/verify",
        headers=headers,
        json={
            "password": PASSWORD,
            "operation": {"method": "POST", "path": path, "body": body},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "path",
    [
        "/api/pi/turns/t/resume",
        "/api/pi/tasks",
        "/api/pi/tasks/t/update",
        "/api/pi/tasks/t/transition",
        "/api/pi/tasks/t/archive",
        "/api/owner/requests/r/decision",
    ],
)
def test_every_runtime_write_and_owner_decision_requires_exact_single_use_proof(boundary, path):
    client, _, headers, seen, _ = boundary
    body = {"status": "approved"} if "/owner/" in path else {"text": "private content"}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "verification_required"
    assert not seen
    verified = proof(client, headers, path, body)
    checked = {**headers, "X-Conker-Verification": verified["verification_token"]}
    assert client.post(path, json=body, headers=checked).status_code == 200
    assert client.post(path, json=body, headers=checked).status_code == 428
    assert len(seen) == 1
    assert "x-conker-verification" not in seen[0].headers
    assert PASSWORD not in seen[0].content.decode()


@pytest.mark.parametrize(
    "path",
    [
        "/api/pi/sessions",
        "/api/pi/sessions/s/turns",
        "/api/pi/sessions/s/fork",
        "/api/pi/turn-submissions/r/cancel",
        "/api/pi/proposals/p/decision",
    ],
)
def test_conversation_writes_need_the_signed_in_session_not_a_password(boundary, path):
    """ADR-0010: chatting and stopping are session-bound; risky writes keep proofs."""
    client, _, headers, seen, _ = boundary
    body = {"text": "private content"}
    assert client.post(path, json=body).status_code == 403
    assert (
        client.post(path, json=body, headers={**headers, "X-CSRF-Token": "x" * 43}).status_code
        == 403
    )
    assert (
        client.post(
            path, json=body, headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post(path + "?extra=1", json=body, headers=headers).status_code == 422
    assert not seen
    assert client.post(path, json=body, headers=headers).status_code == 200
    assert len(seen) == 1 and "x-conker-verification" not in seen[0].headers
    client.cookies.clear()
    assert client.post(path, json=body, headers=headers).status_code == 401
    assert len(seen) == 1


def test_canonical_binding_covers_full_body_identity_revision_and_route(boundary):
    client, _, headers, seen, _ = boundary
    path = "/api/pi/tasks/s/update"
    body = {
        "text": "private",
        "request_id": "request_identity_001",
        "task_id": "task",
        "task_expected_revision": 2,
        "prefer_local": False,
        "nested": {"b": 2, "a": 1},
    }
    checked = {
        **headers,
        "X-Conker-Verification": proof(client, headers, path, body)["verification_token"],
    }
    for altered in (
        {**body, "text": "changed"},
        {**body, "task_expected_revision": 3},
        {**body, "request_id": "request_identity_002"},
        {**body, "prefer_local": True},
    ):
        assert client.post(path, json=altered, headers=checked).status_code == 428
    assert client.post("/api/pi/tasks/other/update", json=body, headers=checked).status_code == 428
    assert client.post(path + "?ignored=1", json=body, headers=checked).status_code == 422
    assert not seen
    # Object key order is not authority; semantic types, arrays and all values are bound.
    assert (
        client.post(path, json=dict(reversed(list(body.items()))), headers=checked).status_code
        == 200
    )


def test_successful_verifications_do_not_exhaust_budget_and_failures_survive_restart(boundary):
    client, auth, headers, _, now = boundary
    for _ in range(8):
        proof(client, headers, "/api/pi/sessions", {})
    token = client.cookies.get(COOKIE)
    for _ in range(5):
        with pytest.raises(AuthError) as error:
            auth.verify(token, "incorrect", "testclient", "binding")
        assert error.value.status == 401
    restarted = AuthStore(auth.path, clock=lambda: now[0], idle_seconds=60)
    with pytest.raises(AuthError) as error:
        restarted.verify(token, PASSWORD, "testclient", "binding")
    assert error.value.status == 429


def test_reads_do_not_extend_unlock_and_expired_verification_cannot_unlock(boundary):
    client, _, headers, seen, now = boundary
    assert client.get("/auth/session").json()["unlock_expires_at"] == 1060
    for instant in (1020, 1040, 1059):
        now[0] = instant
        assert client.get("/api/pi/sessions").status_code == 200
        assert client.get("/auth/session").json()["unlock_expires_at"] == 1060
    now[0] = 1060
    assert client.get("/api/pi/sessions").status_code == 401
    assert client.post("/auth/verify", headers=headers, json={}).status_code == 401
    current = client.get("/auth/session").json()
    assert current["authenticated"] is False and current["unlock_expires_at"] is None
    assert len(seen) == 3


def test_verify_extends_window_without_rotating_and_proof_expiry_is_not_extended(boundary):
    client, auth, headers, _, now = boundary
    token = client.cookies.get(COOKIE)
    initial = client.get("/auth/session").json()
    first = proof(client, headers, "/api/pi/sessions", {})
    now[0] = 1050
    second = proof(client, headers, "/api/pi/sessions", {})
    assert second["unlock_expires_at"] == 1110
    assert client.cookies.get(COOKIE) == token
    current = client.get("/auth/session").json()
    assert current["csrf_token"] == initial["csrf_token"]
    assert current["session_id"] == initial["session_id"]
    now[0] = 1060
    with pytest.raises(AuthError) as error:
        auth.consume(
            token, first["verification_token"], fingerprint("POST", "/api/pi/sessions", {})
        )
    assert error.value.status == 428


@pytest.mark.parametrize("change", ["logout", "reset", "expiry"])
def test_lock_or_reset_during_password_check_cannot_issue_proof(boundary, monkeypatch, change):
    client, auth, _, _, now = boundary
    token = client.cookies.get(COOKIE)
    entered, release = threading.Event(), threading.Event()
    original = module.password_hash

    def delayed(password, salt):
        result = original(password, salt)
        if threading.current_thread().name.startswith("verify"):
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(module, "password_hash", delayed)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="verify") as pool:
        result = pool.submit(auth.verify, token, PASSWORD, "source", "binding")
        try:
            assert entered.wait(5)
            if change == "logout":
                auth.revoke(auth.session(token)["id"])
            elif change == "reset":
                auth.set_password("the replacement password for recovery")
            else:
                now[0] = 1060
        finally:
            release.set()
        with pytest.raises(AuthError):
            result.result()
    with sqlite3.connect(auth.path) as db:
        assert db.execute("SELECT count(*) FROM verification_proofs").fetchone()[0] == 0


def test_concurrent_consumption_admits_exactly_once_and_stores_only_hashes(boundary):
    client, auth, headers, _, _ = boundary
    body = {"title": "private title not stored with proof"}
    verified = proof(client, headers, "/api/pi/sessions", body)
    token = client.cookies.get(COOKIE)
    binding = fingerprint("POST", "/api/pi/sessions", body)
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait(5)
        try:
            auth.consume(token, verified["verification_token"], binding)
            return "admitted"
        except AuthError as error:
            return error.status

    with sqlite3.connect(auth.path) as db:
        dump = "\n".join(db.iterdump())
    assert PASSWORD not in dump and body["title"] not in dump
    assert verified["verification_token"] not in dump and token not in dump
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(consume) for _ in range(2)]
        assert sorted((f.result() for f in futures), key=str) == [428, "admitted"]


def test_upstream_unknown_result_spends_proof_without_retry(boundary):
    client, _, headers, seen, _ = boundary
    path, body = "/api/pi/tasks/failure/update", {"text": "private"}
    checked = {
        **headers,
        "X-Conker-Verification": proof(client, headers, path, body)["verification_token"],
    }
    response = client.post(path, json=body, headers=checked)
    assert response.status_code == 503 and "private" not in response.text
    assert client.post(path, json=body, headers=checked).status_code == 428
    assert len(seen) == 1


def test_proof_cannot_cross_sessions_or_survive_revocation_and_absolute_expiry(boundary):
    client, auth, headers, _, now = boundary
    auth.idle = 300
    path = "/api/pi/sessions"
    binding = fingerprint("POST", path, {})
    verified = proof(client, headers, path, {})
    assert verified["verification_expires_at"] == now[0] + 120
    token = client.cookies.get(COOKIE)
    other = auth.login(auth.anonymous()["token"], PASSWORD, "other")
    with pytest.raises(AuthError) as error:
        auth.consume(other["token"], verified["verification_token"], binding)
    assert error.value.status == 428
    auth.revoke(auth.session(token)["id"])
    with pytest.raises(AuthError) as error:
        auth.consume(token, verified["verification_token"], binding)
    assert error.value.status == 401
    # A fresh verification can never extend the absolute lifetime.
    with auth.transaction() as db:
        db.execute("UPDATE sessions SET expires=? WHERE id=?", (now[0] + 10, other["id"]))
    last = auth.verify(other["token"], PASSWORD, "other", binding)
    assert last["unlock_expires_at"] == last["verification_expires_at"] == now[0] + 10
    now[0] += 10
    with pytest.raises(AuthError):
        auth.consume(other["token"], last["verification_token"], binding)


def test_verify_requires_origin_csrf_and_explicit_allowed_operation(boundary):
    client, _, headers, seen, _ = boundary
    body = {
        "password": PASSWORD,
        "operation": {
            "method": "POST",
            "path": "/api/pi/sessions",
            "body": {},
        },
    }
    for invalid in (
        {},
        {**headers, "Origin": "https://elsewhere.invalid"},
        {**headers, "X-CSRF-Token": "incorrect"},
    ):
        assert client.post("/auth/verify", json=body, headers=invalid).status_code == 403
    for method, path, payload in (
        ("GET", "/api/pi/sessions", {}),
        ("POST", "/api/pi/admin", {}),
        ("POST", "/api/pi/sessions?x=1", {}),
        ("POST", "/api/pi/sessions", []),
        ([], None, {}),
    ):
        response = client.post(
            "/auth/verify",
            headers=headers,
            json={
                "password": PASSWORD,
                "operation": {"method": method, "path": path, "body": payload},
            },
        )
        assert response.status_code == 422 and PASSWORD not in response.text
    assert not seen


@pytest.mark.parametrize(
    "raw",
    [
        '{"password":"one","password":"two"}',
        '{"a":NaN}',
        '{"a":1e999}',
        '{"a":"\\ud800"}',
        '{"a":' + "[" * 40 + "0" + "]" * 40 + "}",
    ],
)
def test_ambiguous_or_unbounded_json_rejected_without_echo(boundary, raw):
    client, _, headers, seen, _ = boundary
    response = client.post(
        "/auth/verify", content=raw, headers={**headers, "Content-Type": "application/json"}
    )
    assert response.status_code == 422 and not seen
    assert "one" not in response.text and "two" not in response.text


def test_config_timeout_validation_and_existing_session_migration(tmp_path):
    for seconds in (0, 59, 86401, True, "60"):
        with pytest.raises(ValueError):
            Config(ORIGIN, "unused", "http://pi", "r" * 32, idle_timeout_seconds=seconds).validate()
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY,id TEXT UNIQUE,csrf TEXT,"
            "authenticated INTEGER,generation INTEGER,created REAL,touched REAL,expires REAL)"
        )
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
            (module.digest("old"), "old", "csrf", 1, 1, 100, 100, 9999),
        )
    auth = AuthStore(path, clock=lambda: 101)
    with pytest.raises(AuthError):
        auth.session("old")
