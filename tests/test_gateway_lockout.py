"""Strangers guessing his password cannot lock him out, and every wrong password reaches his Inbox."""

import hashlib
import sqlite3

import pytest
from test_gateway_mcp import ORIGIN, PASSWORD, boundary  # noqa: F401 - fixture

from gateway import store as module
from gateway.api import COOKIE
from gateway.auth_routes import device_cookie
from gateway.store import AuthError, AuthStore

DEVICE = device_cookie(COOKIE)


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def auth(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(
        module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    store = AuthStore(tmp_path / "auth.db", clock=lambda: clock[0])
    store.set_password(PASSWORD, initial=True)
    return store


def sign_in(auth, password=PASSWORD, source="home", device=""):
    return auth.login(auth.anonymous()["token"], password, source, device)


def refused(call, status):
    with pytest.raises(AuthError) as error:
        call()
    assert error.value.status == status


def flood(auth):
    """Thirty wrong passwords from six strangers fill the shared budget."""
    for stranger in range(6):
        for _ in range(5):
            refused(lambda s=stranger: sign_in(auth, "guess", f"stranger-{s}"), 401)
    refused(lambda: sign_in(auth, source="a new place"), 429)


def test_a_signed_in_tab_still_confirms_while_strangers_fill_the_shared_budget(auth):
    owner = sign_in(auth)
    flood(auth)
    proof = auth.verify(owner["token"], PASSWORD, "home", "decision")
    assert proof["verification_token"]


def test_a_browser_that_signed_in_before_still_signs_in_during_a_flood(auth):
    device = sign_in(auth)["device"]
    flood(auth)
    assert sign_in(auth, device=device)["authenticated"]
    # A made-up or too long device token is a stranger: it draws from the shared budget.
    refused(lambda: sign_in(auth, device="x" * 43), 429)
    refused(lambda: sign_in(auth, device=device + "x" * 128), 429)


def test_a_known_browser_and_a_signed_in_tab_each_get_five_tries(auth, clock):
    owner = sign_in(auth)
    device = owner["device"]
    for _ in range(5):
        refused(lambda: sign_in(auth, "typo", device=device), 401)
    refused(lambda: sign_in(auth, device=device), 429)
    for _ in range(5):
        refused(lambda: auth.verify(owner["token"], "typo", "home", "decision"), 401)
    refused(lambda: auth.verify(owner["token"], PASSWORD, "home", "decision"), 429)
    # The shared budget was never touched, and the tries come back after five minutes.
    assert sign_in(auth, source="elsewhere")["authenticated"]
    clock[0] += 301
    assert sign_in(auth, device=device)["authenticated"]
    assert auth.verify(owner["token"], PASSWORD, "home", "decision")["verification_token"]


def test_each_sign_in_replaces_the_device_token_and_only_its_hash_is_kept(auth):
    first = sign_in(auth)["device"]
    second = sign_in(auth, device=first)["device"]
    assert second != first
    with sqlite3.connect(auth.path) as db:
        dump = "\n".join(db.iterdump())
    assert first not in dump and second not in dump
    flood(auth)
    refused(lambda: sign_in(auth, device=first), 429)
    assert sign_in(auth, device=second)["authenticated"]


@pytest.mark.parametrize("forget", ["sign out everywhere", "new password"])
def test_signing_out_everywhere_or_a_new_password_forgets_every_browser(auth, forget):
    device = sign_in(auth)["device"]
    if forget == "sign out everywhere":
        auth.revoke()
    else:
        auth.set_password(PASSWORD + " again")
    flood(auth)
    refused(lambda: sign_in(auth, device=device), 429)


def test_a_browser_unused_for_400_days_is_forgotten(auth, clock):
    device = sign_in(auth)["device"]
    clock[0] += 400 * 86400
    flood(auth)
    refused(lambda: sign_in(auth, device=device), 429)


def test_every_wrong_password_reaches_his_alerts_until_he_has_seen_it(auth, clock):
    owner = sign_in(auth)
    assert auth.alerts() == {"unseen": 0, "results": []}
    refused(lambda: sign_in(auth, "guess", "stranger"), 401)
    clock[0] += 1
    refused(lambda: sign_in(auth, "typo", device=owner["device"]), 401)
    clock[0] += 1
    refused(lambda: auth.verify(owner["token"], "typo", "home", "decision"), 401)
    alerts = auth.alerts()
    assert alerts["unseen"] == 3
    assert [(row["origin"], row["seen"]) for row in alerts["results"]] == [
        ("signed_in", False),
        ("known_device", False),
        ("new_device", False),
    ]
    assert alerts["results"][0]["at"] == clock[0]
    # Right passwords and refused (over budget) attempts are not wrong passwords.
    auth.verify(owner["token"], PASSWORD, "home", "decision")
    assert auth.alerts()["unseen"] == 3
    auth.alerts_seen()
    assert auth.alerts()["unseen"] == 0
    assert all(row["seen"] for row in auth.alerts()["results"])
    clock[0] += 1
    refused(lambda: sign_in(auth, "guess", "stranger"), 401)
    assert auth.alerts()["unseen"] == 1


def test_alerts_keep_30_days_and_at_most_500(auth, clock):
    refused(lambda: sign_in(auth, "guess", "stranger"), 401)
    clock[0] += 30 * 86400
    assert auth.alerts()["unseen"] == 0
    with sqlite3.connect(auth.path) as db:
        db.executemany(
            "INSERT INTO password_failures(at,origin) VALUES(?,?)",
            [(clock[0] - n, "new_device") for n in range(600)],
        )
    alerts = auth.alerts()
    assert alerts["unseen"] == 500 and len(alerts["results"]) == 50
    assert alerts["results"][0]["at"] == clock[0]


def test_sign_in_gives_the_browser_a_lasting_device_cookie_for_auth_only(boundary):
    _, client, _, _, _ = boundary
    assert DEVICE == "__Secure-conker-device"
    client.cookies.clear()
    csrf = client.get("/auth/session").json()["csrf_token"]
    response = client.post(
        "/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    (device,) = [
        value for value in response.headers.get_list("set-cookie") if value.startswith(DEVICE + "=")
    ]
    attributes = {part.strip().lower() for part in device.split(";")[1:]}
    assert {"secure", "httponly", "samesite=strict", "path=/auth"} <= attributes
    assert f"max-age={400 * 86400}" in attributes
    assert "device" not in response.json()


def test_alerts_need_his_session_and_seen_takes_an_empty_object(boundary):
    app, client, headers, _, _ = boundary
    refused(lambda: app.state.auth.verify("not a session", "guess", "x", "y"), 401)
    token = client.cookies.get(COOKIE)
    refused(lambda: app.state.auth.verify(token, "typo", "home", "decision"), 401)
    response = client.get("/auth/alerts")
    assert response.status_code == 200 and response.json()["unseen"] == 1
    assert response.json()["results"][0]["origin"] == "signed_in"
    for wrong in (
        {},
        {**headers, "Origin": "https://elsewhere.test"},
        {**headers, "X-CSRF-Token": "x"},
    ):
        assert client.post("/auth/alerts/seen", json={}, headers=wrong).status_code == 403
    assert client.post("/auth/alerts/seen", json={"at": 1}, headers=headers).status_code == 422
    assert client.post("/auth/alerts/seen?all=1", json={}, headers=headers).status_code == 422
    assert client.get("/auth/alerts").json()["unseen"] == 1
    assert client.post("/auth/alerts/seen", json={}, headers=headers).json() == {"seen": True}
    assert client.get("/auth/alerts").json()["unseen"] == 0
    client.cookies.clear()
    assert client.get("/auth/alerts").status_code == 401
