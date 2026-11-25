"""A device belongs to a sign-in that exists and follows its own browser to the next one
(gateway/push.py Devices.add, gateway/known_browsers.py)."""

import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from test_gateway_push import (
    FCM,
    MOZILLA,
    ORIGIN,
    device,
    sign_in,
    subscribe,
    world,  # noqa: F401  (a fixture)
)

from gateway import known_browsers, push
from gateway import store as auth_module
from gateway.auth_routes import device_cookie


def test_a_device_is_never_put_on_a_sign_in_that_was_signed_out(world):
    _, _, _, app, _ = world
    _, p256dh, secret = device()
    gone = app.state.auth.sessions()[0]["id"]
    app.state.auth.revoke(gone)  # sign-out lands while the request waits for its body
    for renew in (False, True):
        with pytest.raises(auth_module.AuthError) as refused:
            push.Devices(app.state.auth).add(
                FCM, p256dh, secret, session=gone, key="k", label="Chrome", renew=renew
            )
        assert refused.value.status == 401
    assert push.Devices(app.state.auth).ids() == []


def remembered(client, app):
    """This browser's known-device cookie (sent to /auth only)."""
    name = device_cookie(app.state.config.cookie_name)
    return next(cookie for cookie in client.cookies.jar if cookie.name == name)


def idle_out(app, hours: float = 1) -> None:
    later = time.time() + hours * 3600
    app.state.auth.clock = lambda: later


def test_a_device_follows_its_browser_to_the_next_sign_in_without_a_renewal(world):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    idle_out(app)  # the sign-in idled out; the page never sends the subscription again
    again = sign_in(client)
    assert len(push.Devices(app.state.auth).ids()) == 1  # idling out never turns it off
    assert client.post("/auth/logout", headers=again).status_code == 200
    assert push.Devices(app.state.auth).ids() == []  # signing out where it went turns it off


def test_only_the_browser_that_turned_a_device_on_carries_it(world):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    first = remembered(client, app).value
    idle_out(app, 1)
    sign_in(client)  # the same browser: the device moves to this sign-in
    carried = app.state.auth.sessions()[0]["id"]
    # Neither a token this browser has since been given a new one for, nor a browser that never
    # signed in here, carries it on.
    remembered(client, app).value = first
    idle_out(app, 2)
    assert client.post("/auth/logout", headers=sign_in(client)).status_code == 200
    client.cookies.jar.clear(name=device_cookie(app.state.config.cookie_name), **where(client, app))
    idle_out(app, 3)
    assert client.post("/auth/logout", headers=sign_in(client)).status_code == 200
    assert len(push.Devices(app.state.auth).ids()) == 1
    app.state.auth.revoke(carried)  # signing out the sign-in it moved to turns it off
    assert push.Devices(app.state.auth).ids() == []


def where(client, app) -> dict:
    cookie = remembered(client, app)
    return {"domain": cookie.domain, "path": cookie.path}


def test_a_token_an_older_gateway_replaced_carries_nothing(world):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    first = app.state.auth.sessions()[0]["id"]
    # After a rollback, the older gateway's sign-in replaced the token and left its link behind.
    with app.state.auth.transaction() as db:
        db.execute("DELETE FROM known_devices")
    idle_out(app)
    assert client.post("/auth/logout", headers=sign_in(client)).status_code == 200
    assert len(push.Devices(app.state.auth).ids()) == 1  # still on its own, idled-out sign-in
    with app.state.auth.transaction(write=False) as db:
        assert db.execute("SELECT count(*) FROM known_device_signins").fetchone()[0] == 1
    app.state.auth.revoke(first)
    assert push.Devices(app.state.auth).ids() == []


def test_a_gateway_database_from_before_refused_sends_nothing_and_keeps_running(world):
    client, _, state, app, folder = world
    _, p256dh, secret = device()
    session = app.state.auth.sessions()[0]["id"]
    key = push.public_key_id(push.server_key(folder))
    # A database made by an earlier build of web push: no `refused` column.
    with app.state.auth.transaction() as db:
        db.execute("DROP TABLE push_subscriptions")
        db.execute(
            "CREATE TABLE push_subscriptions (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL UNIQUE,"
            " p256dh TEXT NOT NULL, auth TEXT NOT NULL, created REAL NOT NULL,"
            " session_id TEXT NOT NULL, key_id TEXT NOT NULL, label TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO push_subscriptions VALUES(?,?,?,?,?,?,?,?)",
            ("a" * 32, FCM, p256dh, secret, 1.0, session, key, "Chrome"),
        )
    state.claim = {"items": [{"kind": "approval", "urgent": True, "tag": "b" * 16}]}
    with pytest.raises(sqlite3.OperationalError):
        app.state.push.tick()  # the round fails before anything is claimed or sent
    assert state.pushes == [] and state.pi == []
    app.state.push.start()  # a restart still starts
    assert [row["id"] for row in client.get("/api/push").json()["devices"]] == ["a" * 32]
    idle_out(app)
    again = sign_in(client)  # signing in and out still work on such a database
    assert client.post("/auth/logout", headers=again).status_code == 200
    assert push.Devices(app.state.auth).ids() == []


def test_a_sign_in_moves_only_its_own_browsers_devices(world):
    first, headers, _, app, _ = world
    second = TestClient(app, base_url=ORIGIN)  # another browser, with its own cookies
    one, two = device(), device()
    subscribe(first, headers, FCM, one[1], one[2])
    subscribe(second, sign_in(second), MOZILLA, two[1], two[2])
    sign_in(first)  # the first browser signs in again: its device moves, the other's stays
    assert second.post("/auth/logout", headers=sign_in_headers(second)).status_code == 200
    on = [row["id"] for row in push.Devices(app.state.auth).ids()]
    assert on == [hashlib.sha256(FCM.encode()).hexdigest()[:32]]


def sign_in_headers(client) -> dict:
    """A write's headers for the sign-in this browser already has."""
    token = client.get("/auth/session").json()["csrf_token"]
    return {"Origin": ORIGIN, "X-CSRF-Token": token}


def test_only_a_link_whose_token_is_still_remembered_carries_devices(tmp_path):
    auth = auth_module.AuthStore(tmp_path / "auth.db")
    row = "INSERT INTO push_subscriptions VALUES(?,?,?,?,?,?,?,?,0)"
    with auth.transaction() as db:
        # Straight to the sign-in step, without the prune a sign-in runs first: one link's token
        # is still remembered, the other's was replaced (by an older gateway, say).
        db.execute("INSERT INTO known_devices VALUES('here',1,1)")
        db.execute("INSERT INTO known_device_signins VALUES('here','last'),('gone','stale')")
        db.execute(row, ("a" * 32, FCM, "p", "s", 1.0, "last", "k", "Chrome"))
        db.execute(row, ("b" * 32, MOZILLA, "p", "s", 1.0, "stale", "k", "Firefox"))
        known_browsers.signed_in(db, "gone", "next", "new", 2.0)
        known_browsers.signed_in(db, "here", "kept", "now", 3.0)
        devices = db.execute("SELECT id,session_id FROM push_subscriptions ORDER BY id")
        assert [tuple(device) for device in devices] == [("a" * 32, "now"), ("b" * 32, "stale")]
        links = db.execute("SELECT * FROM known_device_signins ORDER BY token_hash")
        assert [tuple(link) for link in links] == [("kept", "now"), ("next", "new")]
