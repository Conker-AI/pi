"""`python -m gateway owner-preferences`: the host CLI's way to save a security preference.

Pi refuses a change to the sign-out time, a new chat's access or the daily budgets on its recovery
key (tests/test_security_preferences_password.py). `conker config set` then sends the same save
here with the owner's password on stdin: the password is checked with the host budget, and the
save goes to Pi with the owner credential at the revision the CLI read.
"""

import copy
import hashlib
import io
import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import host_preferences as host
from gateway import store as module
from gateway.store import AuthError, AuthStore
from pi.api import server as api
from pi.conversation.store import Store
from pi.core import owner_preferences as prefs

OWNER = "gateway-host-preferences-owner-key-" + "o" * 32
PASSWORD = "correct horse battery staple"


def fast(value, salt):
    return hashlib.sha256(salt + value.encode()).hexdigest()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


@pytest.fixture
def pi(monkeypatch, store):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "gateway-host-preferences-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    return TestClient(api.app)


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "password_hash", fast)
    value = AuthStore(tmp_path / "auth.db")
    value.set_password(PASSWORD, initial=True)
    return value


class Config:
    pi_url = "http://pi.invalid"
    pi_owner_key = OWNER


def through(pi, seen=None):
    """An httpx client whose requests reach the in-process Pi app."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        reply = pi.request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def save(store, **changes):
    current = prefs.load(store)
    preferences = copy.deepcopy(current["preferences"])
    preferences.update(changes)
    return {"expected_revision": current["revision"], "preferences": preferences}


def ask(auth, client, payload) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    code = host.run(Config(), auth, client, io.BytesIO(raw), out, err)
    return code, out.getvalue(), err.getvalue()


def test_the_right_password_saves_with_the_owner_credential(pi, store, auth):
    body = save(store, idleTimeoutMinutes=60, newChatAccess="full")
    body["preferences"]["dailyBudget"]["costCents"] = 2500
    seen = []
    with through(pi, seen) as client:
        code, out, err = ask(auth, client, {"password": PASSWORD, **body})
    assert (code, err) == (0, "")
    assert json.loads(out) == {"revision": 2, "preferences": body["preferences"]}
    assert prefs.load(store) == {"revision": 2, "preferences": body["preferences"]}
    [sent] = seen
    assert sent.method == "POST" and sent.headers["x-pi-owner-key"] == OWNER
    assert json.loads(sent.content) == body  # the CLI's own save, and never the password
    assert PASSWORD.encode() not in sent.content
    # The gateway enforces the sign-out time saved from then on.
    with auth.transaction() as db:
        assert db.execute("SELECT revision,minutes FROM owner_window").fetchone()[:] == (2, 60)


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "wrong password, nearly right"},
        {"password": ""},
        {"password": 7},
        {"password": PASSWORD, "expected_revision": "1"},
        {"password": PASSWORD, "expected_revision": True},
        {"password": PASSWORD, "expected_revision": 0},
        {"password": PASSWORD, "preferences": ["not", "a", "document"]},
        {"password": PASSWORD, "extra": 1},
        {"password": PASSWORD, "spendingCap": {"cents": 100_000}},
        {"password": PASSWORD, "idleTimeoutMinutes": 7},  # Pi's own model refuses it
        {"password": PASSWORD, "newChatAccess": "everything"},
    ],
)
def test_anything_else_is_refused_and_saves_nothing(pi, store, auth, payload):
    body = save(store, idleTimeoutMinutes=60)
    password = payload.pop("password")
    for key, value in payload.items():
        if key in body:
            body[key] = value
        elif key in body["preferences"]:
            body["preferences"][key] = value
        else:
            body[key] = value
    before = prefs.load(store)
    with through(pi) as client:
        code, out, err = ask(auth, client, {"password": password, **body})
    assert code == 1 and out == "" and err.strip()
    assert PASSWORD not in err and "nearly right" not in err and "everything" not in err
    assert prefs.load(store) == before


@pytest.mark.parametrize(
    "raw",
    [
        b"not json " + PASSWORD.encode(),
        b'{"password": "' + PASSWORD.encode() + b'"' + b" " * 17000 + b"}",
        b"[]",
    ],
)
def test_a_malformed_request_is_refused_without_repeating_it(pi, store, auth, raw):
    before = prefs.load(store)
    with through(pi) as client:
        code, out, err = ask(auth, client, raw)
    assert code == 1 and out == "" and PASSWORD not in err and "Nothing was saved" in err
    assert prefs.load(store) == before


def test_a_save_made_meanwhile_is_refused_never_overwritten(pi, store, auth):
    stale = save(store, idleTimeoutMinutes=60)
    with through(pi) as client:
        assert ask(auth, client, {"password": PASSWORD, **save(store, urgency="off")})[0] == 0
        code, out, err = ask(auth, client, {"password": PASSWORD, **stale})
    assert code == 1 and out == "" and "changed while" in err
    assert prefs.load(store)["preferences"]["idleTimeoutMinutes"] == 15


def test_wrong_passwords_share_the_host_budget(pi, store, auth):
    with through(pi) as client:
        for _ in range(5):
            assert ask(auth, client, {"password": "x" * 20, **save(store)})[0] == 1
        code, _, err = ask(auth, client, {"password": PASSWORD, **save(store, newChatAccess="ai")})
    assert code == 1 and "Too many password attempts. Wait 5 minutes and retry." in err
    with pytest.raises(AuthError, match="Wait 5 minutes"):
        auth.host_password(PASSWORD)  # spending-cap and apply approvals wait too
    assert prefs.load(store)["revision"] == 1


def test_pi_silence_is_reported_without_the_body(auth, store):
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    body = {"password": PASSWORD, **save(store, idleTimeoutMinutes=60)}
    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        code, _, err = ask(auth, client, body)
    assert code == 1 and "Nothing was saved" in err and PASSWORD not in err

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer")

    with httpx.Client(transport=httpx.MockTransport(lost)) as client:
        code, _, err = ask(auth, client, body)
    assert code == 1 and "may already be saved; check with conker config get" in err

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"input": PASSWORD, "msg": "no"}]})

    with httpx.Client(transport=httpx.MockTransport(refused)) as client:
        code, _, err = ask(auth, client, body)
    assert code == 1 and "HTTP 422" in err and PASSWORD not in err


def test_the_command_is_wired_and_reads_stdin(monkeypatch):
    from gateway import __main__ as entry

    seen = []
    monkeypatch.setattr(host, "main", lambda db: seen.append(db) or 0)
    monkeypatch.setattr("sys.argv", ["gateway", "owner-preferences", "--db", "/tmp/auth.db"])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 0 and seen == ["/tmp/auth.db"]
