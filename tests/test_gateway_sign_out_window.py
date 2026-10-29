"""The owner's sign-out window: his choice in Pi's preferences, never past the host's ceiling."""

import copy
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthError, AuthStore
from pi.owner import preferences as prefs

ORIGIN = "https://localhost:8050"
PASSWORD = "sign out window testing phrase"
OWNER = "sign-out-window-owner-key-" + "o" * 32
PATH = "/api/control/pi/owner/preferences"


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
    value = AuthStore(tmp_path / "auth.db", clock=lambda: clock[0])
    value.set_password(PASSWORD)
    return value


def document(revision, minutes):
    preferences = copy.deepcopy(prefs.DEFAULT)
    preferences["idleTimeoutMinutes"] = minutes
    return {"revision": revision, "preferences": preferences}


def test_the_owner_choice_shortens_every_sign_in_and_survives_a_restart(auth, clock):
    session = auth.login(auth.anonymous()["token"], PASSWORD, "local")
    assert session["unlock_expires_at"] == clock[0] + 1800
    assert auth.owner_window(2, 15, saved=False)
    # Already signed-in browsers follow at once.
    assert auth.session(session["token"])["unlock_expires_at"] == clock[0] + 900
    restarted = AuthStore(auth.path, clock=lambda: clock[0])
    assert restarted.session(session["token"])["unlock_expires_at"] == clock[0] + 900
    clock[0] += 900
    with pytest.raises(AuthError):
        restarted.session(session["token"])
    assert restarted.sessions() == []


def test_the_host_ceiling_holds_and_zero_means_the_host_limit(auth, clock):
    session = auth.login(auth.anonymous()["token"], PASSWORD, "local")
    for minutes in (60, 0):
        assert auth.owner_window(3, minutes, saved=True)
        assert auth.session(session["token"])["unlock_expires_at"] == clock[0] + 1800
    auth.idle = 120
    assert auth.session(session["token"])["unlock_expires_at"] == clock[0] + 120


def test_a_late_read_never_loosens_and_a_save_always_wins(auth, clock):
    session = auth.login(auth.anonymous()["token"], PASSWORD, "local")

    def window():
        return auth.session(session["token"])["unlock_expires_at"] - clock[0]

    assert auth.owner_window(5, 5, saved=True) and window() == 300
    # A read that raced the save still shows the older, longer choice: it is not kept.
    assert not auth.owner_window(4, 30, saved=False)
    assert not auth.owner_window(4, 0, saved=False)
    assert window() == 300
    # An older read may only shorten it.
    assert auth.owner_window(4, 2, saved=False) and window() == 120
    assert auth.owner_window(6, 15, saved=False) and window() == 900
    # A save is his latest choice, even after Pi was restored with older revisions.
    assert auth.owner_window(1, 30, saved=True) and window() == 1800


@pytest.mark.parametrize(
    "revision,minutes",
    [(0, 5), (True, 5), (None, 5), (6, True), (6, -1), (6, 10081), (6, "5"), (6, 5.0)],
)
def test_anything_but_a_number_of_minutes_changes_nothing(auth, clock, revision, minutes):
    session = auth.login(auth.anonymous()["token"], PASSWORD, "local")
    assert auth.owner_window(2, 15, saved=True)
    assert not auth.owner_window(revision, minutes, saved=True)
    assert auth.session(session["token"])["unlock_expires_at"] == clock[0] + 900


def gateway(auth, answers):
    seen = []

    def upstream(request):
        seen.append(request)
        status, body = answers(request)
        return httpx.Response(status, json=body)

    config = Config(ORIGIN, str(auth.path), "http://pi:8050", "r" * 32, pi_owner_key=OWNER)
    app = create_app(config, store=auth, transport=httpx.MockTransport(upstream))
    return TestClient(app, base_url=ORIGIN), seen


def sign_in(client):
    csrf = client.get("/auth/session").json()["csrf_token"]
    login = client.post(
        "/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert login.status_code == 200, login.text
    return login.json(), {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}


def verified(client, headers, body):
    proof = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": "POST", "path": PATH, "body": body}},
    )
    assert proof.status_code == 200, proof.text
    return {**headers, "X-Conker-Verification": proof.json()["verification_token"]}


def test_the_gateway_learns_the_choice_from_preferences_it_relays(auth, clock):
    stored = document(3, 5)

    def answers(request):
        if request.method == "POST":
            sent = json.loads(request.content)
            return 200, {
                "revision": sent["expected_revision"] + 1,
                "preferences": sent["preferences"],
            }
        return 200, stored

    client, seen = gateway(auth, answers)
    with client:
        login, headers = sign_in(client)
        assert login["unlock_expires_at"] == clock[0] + 1800
        # Opening Settings reads his preferences; the document passes through unchanged.
        assert client.get(PATH).json() == stored
        assert client.get("/auth/session").json()["unlock_expires_at"] == clock[0] + 300
        clock[0] += 300
        assert client.get(PATH).status_code == 401
        login, headers = sign_in(client)
        assert login["unlock_expires_at"] == clock[0] + 300
        # Saving a longer window asks for the password; the host's 30 minutes still cap it.
        body = {"expected_revision": 3, "preferences": document(3, 60)["preferences"]}
        assert client.post(PATH, json=body, headers=headers).status_code == 428
        checked = verified(client, headers, body)
        assert client.post(PATH, json=body, headers=checked).status_code == 200
        assert client.get("/auth/session").json()["unlock_expires_at"] == clock[0] + 1800
    assert [request.method for request in seen] == ["GET", "POST"]
    assert all(request.headers["x-pi-owner-key"] == OWNER for request in seen)


@pytest.mark.parametrize(
    "status,body",
    [
        (409, {"detail": {"code": "revision_conflict", "current_revision": 9}}),
        (200, {"revision": 9, "preferences": None}),
        (200, {"revision": 9, "configuration": None}),
        (200, ["not", "a", "document"]),
        (500, document(9, 5)),
    ],
)
def test_a_failed_or_unreadable_answer_changes_nothing(auth, clock, status, body):
    client, seen = gateway(auth, lambda request: (status, body))
    with client:
        sign_in(client)
        assert client.get(PATH).status_code == status
        assert client.get("/auth/session").json()["unlock_expires_at"] == clock[0] + 1800
    assert len(seen) == 1


def test_other_owner_documents_never_set_the_window(auth, clock):
    client, _ = gateway(auth, lambda request: (200, document(9, 5)))
    with client:
        sign_in(client)
        assert client.get("/api/control/pi/owner/settings").status_code == 200
        assert client.get("/auth/session").json()["unlock_expires_at"] == clock[0] + 1800
