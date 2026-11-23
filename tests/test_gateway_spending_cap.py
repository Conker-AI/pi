"""`python -m gateway spending-cap`: the host CLI's one way to change the daily cap (R-M6).

The owner's password and the change arrive together on stdin and are used once: the password is
checked against the gateway's verifier, then the cap is saved with the owner credential at the
revision just read. Pi refuses a cap change on its recovery key (tests/test_spending_cap_password.py).
"""

import copy
import hashlib
import io
import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import spending_cap as host_cap
from gateway import store as module
from gateway.store import AuthError, AuthStore
from pi.api import server as api
from pi.conversation.store import Store
from pi.core import owner_preferences as prefs

OWNER = "gateway-spending-cap-owner-key-" + "o" * 32
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
    monkeypatch.setattr(api.app.state, "admin_key", "gateway-spending-cap-recovery", raising=False)
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


def through(pi):
    """An httpx client whose requests reach the in-process Pi app."""

    def handler(request: httpx.Request) -> httpx.Response:
        reply = pi.request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def ask(auth, client, payload) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    code = host_cap.run(Config(), auth, client, io.BytesIO(raw), out, err)
    return code, out.getvalue(), err.getvalue()


def test_the_right_password_saves_only_the_cap(pi, store, auth):
    with through(pi) as client:
        code, out, err = ask(
            auth, client, {"password": PASSWORD, "changes": {"cents": 900, "enabled": False}}
        )
    assert (code, err) == (0, "")
    saved = json.loads(out)
    assert saved["spendingCap"] == {"enabled": False, "cents": 900, "tokens": 2_000_000}
    assert saved["revision"] == 2
    assert prefs.load(store) == {
        "revision": 2,
        "preferences": {**prefs.DEFAULT, "spendingCap": saved["spendingCap"]},
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "wrong password, nearly right", "changes": {"cents": 100_000}},
        {"password": "", "changes": {"cents": 100_000}},
        {"password": PASSWORD, "changes": {}},
        {"password": PASSWORD, "changes": {"cents": 100_000, "idleTimeoutMinutes": 0}},
        {"password": PASSWORD, "changes": {"cents": "100000"}},
        {"password": PASSWORD, "changes": {"enabled": 0}},
        {"password": PASSWORD, "changes": {"cents": True}},
        {"password": PASSWORD, "changes": {"cents": 100_001}},
        {"password": PASSWORD, "changes": {"tokens": -1}},
        {"password": PASSWORD},
        {"password": PASSWORD, "changes": {"cents": 100_000}, "extra": 1},
        b"not json " + PASSWORD.encode(),
        b'{"password": "' + PASSWORD.encode() + b'", "changes": {"cents": 1' + b" " * 5000 + b"}}",
    ],
)
def test_anything_else_is_refused_and_saves_nothing(pi, store, auth, payload):
    before = prefs.load(store)
    with through(pi) as client:
        code, out, err = ask(auth, client, payload)
    assert code == 1 and out == "" and err.strip()
    assert PASSWORD not in err and "nearly right" not in err and "100000" not in err
    assert prefs.load(store) == before


def test_wrong_passwords_are_limited_and_a_right_one_then_waits(pi, store, auth):
    with through(pi) as client:
        for _ in range(5):
            assert ask(auth, client, {"password": "x" * 20, "changes": {"cents": 1}})[0] == 1
        code, _, err = ask(auth, client, {"password": PASSWORD, "changes": {"cents": 1}})
    assert code == 1 and "Too many password attempts" in err
    assert prefs.load(store)["preferences"]["spendingCap"]["cents"] == 500


def test_right_passwords_never_count_and_a_reset_during_the_check_stops_it(auth, monkeypatch):
    for _ in range(6):
        auth.host_password(PASSWORD)
    resets = []

    def resetting(value, salt):
        if not resets:
            resets.append(True)
            auth.set_password("a different passphrase entirely")
        return fast(value, salt)

    monkeypatch.setattr(module, "password_hash", resetting)
    with pytest.raises(AuthError, match="changed"):
        auth.host_password(PASSWORD)


def test_pi_refusals_and_silence_are_reported_without_the_body(auth):
    def changed(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json={"revision": 3, "preferences": copy.deepcopy(prefs.DEFAULT)}
            )
        return httpx.Response(409, json={"detail": {"message": "changed " + PASSWORD}})

    with httpx.Client(transport=httpx.MockTransport(changed)) as client:
        code, out, err = ask(auth, client, {"password": PASSWORD, "changes": {"cents": 700}})
    assert code == 1 and out == "" and "changed while" in err and PASSWORD not in err

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        code, _, err = ask(auth, client, {"password": PASSWORD, "changes": {"cents": 700}})
    assert code == 1 and "Nothing was saved" in err

    def lost(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json={"revision": 3, "preferences": copy.deepcopy(prefs.DEFAULT)}
            )
        raise httpx.ReadTimeout("no answer")

    with httpx.Client(transport=httpx.MockTransport(lost)) as client:
        code, _, err = ask(auth, client, {"password": PASSWORD, "changes": {"cents": 700}})
    assert code == 1 and "may already" in err


def test_the_command_is_wired_and_reads_stdin(monkeypatch):
    from gateway import __main__ as entry

    seen = []
    monkeypatch.setattr(host_cap, "main", lambda db: seen.append(db) or 0)
    monkeypatch.setattr("sys.argv", ["gateway", "spending-cap", "--db", "/tmp/auth.db"])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 0 and seen == ["/tmp/auth.db"]
