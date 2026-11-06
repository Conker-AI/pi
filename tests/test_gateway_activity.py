"""Idle sign-out: a sign-in stays unlocked while the owner uses the page, and only then.

The window restarts on a password (sign-in, a confirmed action) or an activity mark the page sends
on the owner's own clicks and keys. Reads, polling and writes never extend it (L-03), an expired
sign-in stays expired, and nothing outlives the absolute limit from sign-in.
"""

import hashlib
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthError, AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "idle sign out testing phrase"


@pytest.fixture(autouse=True)
def quick_hash(monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def auth(tmp_path, clock):
    value = AuthStore(
        tmp_path / "auth.db", clock=lambda: clock[0], idle_seconds=600, absolute_seconds=3600
    )
    value.set_password(PASSWORD)
    return value


def signed_in(auth):
    return auth.login(auth.anonymous()["token"], PASSWORD, "local")["token"]


def test_activity_restarts_the_window_and_reads_do_not(auth, clock):
    token = signed_in(auth)
    clock[0] += 500
    # A read (what polling does) neither ends nor extends the window.
    assert auth.session(token)["unlock_expires_at"] == 1600
    assert auth.activity(token) == {"unlock_expires_at": 2100}
    assert auth.session(token)["unlock_expires_at"] == 2100
    clock[0] += 599
    assert auth.session(token)["unlock_expires_at"] == 2100
    clock[0] += 1
    with pytest.raises(AuthError):
        auth.session(token)


def test_an_expired_sign_in_stays_expired(auth, clock):
    token = signed_in(auth)
    clock[0] += 600
    with pytest.raises(AuthError):
        auth.activity(token)
    with pytest.raises(AuthError):
        auth.session(token)
    assert auth.sessions() == []


def test_activity_never_passes_the_absolute_limit(auth, clock):
    token = signed_in(auth)
    for _ in range(6):
        clock[0] += 590
        auth.activity(token)
    # 3540 s after sign-in: the window is capped at sign-in + 3600.
    assert auth.session(token)["unlock_expires_at"] == 4600
    clock[0] = 4600
    with pytest.raises(AuthError):
        auth.activity(token)


def test_the_owner_choice_bounds_the_activity_window(auth, clock):
    token = signed_in(auth)
    assert auth.owner_window(1, 2, saved=True)
    assert auth.activity(token) == {"unlock_expires_at": clock[0] + 120}
    clock[0] += 120
    with pytest.raises(AuthError):
        auth.session(token)


def test_an_anonymous_session_cannot_mark_activity(auth):
    with pytest.raises(AuthError):
        auth.activity(auth.anonymous()["token"])
    with pytest.raises(AuthError):
        auth.activity("not-a-session")


def test_a_database_from_before_activity_gains_the_column_and_keeps_sign_ins(tmp_path, clock):
    path = tmp_path / "auth.db"
    old = AuthStore(path, clock=lambda: clock[0], idle_seconds=600, absolute_seconds=3600)
    old.set_password(PASSWORD)
    token = signed_in(old)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE sessions DROP COLUMN active_at")
    reopened = AuthStore(path, clock=lambda: clock[0], idle_seconds=600, absolute_seconds=3600)
    # The sign-in keeps its password time; activity works on the migrated table.
    assert reopened.session(token)["unlock_expires_at"] == 1600
    clock[0] += 300
    assert reopened.activity(token) == {"unlock_expires_at": 1900}


def test_pruning_keeps_a_session_kept_alive_by_activity(auth, clock):
    token = signed_in(auth)
    clock[0] += 500
    auth.activity(token)
    clock[0] += 500
    # A new sign-in prunes stale sessions; this one was active 500 s ago, so it stays.
    signed_in(auth)
    assert auth.session(token)["unlock_expires_at"] == 2100
    assert len(auth.sessions()) == 2


@pytest.fixture
def gateway(auth):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    config = Config(ORIGIN, str(auth.path), "http://pi:8050", "r" * 32, pi_owner_key="o" * 40)
    app = create_app(config, store=auth, transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert login.status_code == 200, login.text
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        with TestClient(app, base_url=ORIGIN) as anonymous:
            yield client, anonymous, headers, seen


def test_the_activity_route_keeps_the_sign_in_and_needs_csrf_and_origin(gateway, clock):
    client, anonymous, headers, seen = gateway
    assert anonymous.post("/auth/activity", json={}, headers=headers).status_code == 401
    assert client.post("/auth/activity", json={}, headers={"Origin": ORIGIN}).status_code == 403
    foreign = {**headers, "Origin": "https://evil.example"}
    assert client.post("/auth/activity", json={}, headers=foreign).status_code == 403
    assert client.post("/auth/activity", json={"x": 1}, headers=headers).status_code == 422
    assert client.post("/auth/activity?x=1", json={}, headers=headers).status_code == 422
    assert client.post("/auth/activity", content=b"{}", headers=headers).status_code == 415
    clock[0] += 500
    answer = client.post("/auth/activity", json={}, headers=headers)
    assert answer.status_code == 200 and answer.json() == {"unlock_expires_at": 2100}
    assert seen == []
    clock[0] += 599
    assert client.get("/auth/session").json()["authenticated"] is True
    clock[0] += 1
    assert client.post("/auth/activity", json={}, headers=headers).status_code == 401


def test_polling_and_writes_do_not_extend_the_window(gateway, clock):
    client, _, headers, seen = gateway
    for _ in range(5):
        clock[0] += 110
        assert client.get("/api/pi/sessions").status_code == 200
        assert client.get("/auth/session").json()["authenticated"] is True
        # A session-only write is allowed but is not the owner's input either.
        assert client.post("/api/pi/tasks", json={"title": "x"}, headers=headers).status_code == 200
    clock[0] += 50
    assert client.get("/api/pi/sessions").status_code == 401
    assert len(seen) == 10
