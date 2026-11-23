"""`conker apply approvals` asks for exactly what the Inbox asks for (gateway/approval_levels.py).

The browser's decision route (tests/test_gateway_approval_levels.py) asks the password for a yes at
level 3 (delete, pay, change the host) or at a level it cannot read; any other yes, a no and a
dismissal need only the signed-in session. The host command used to decide every one with the
gateway's ToolGate owner key and no password. It now reads the level the same way and refuses
such a yes, deciding nothing, until the owner's password comes with it on stdin.
"""

import hashlib
import io
import json

import httpx
import pytest

from gateway import store as module
from gateway.api import Config
from gateway.mutation import MutationError, apply_resource
from gateway.store import AuthError, AuthStore

PASSWORD = "correct horse battery staple"
VIEW = "http://toolgate.test/v2/owner/requests/req_1"
YES = {"operation": "decide", "id": "req_1", "status": "approved", "note": "pay it"}


def config() -> Config:
    return Config(
        "https://conker.test",
        "/auth/auth.db",
        "http://pi.test",
        "runtime-" + "r" * 32,
        "http://toolgate.test",
        "owner-" + "o" * 32,
        pi_owner_key="control-" + "c" * 32,
    )


def fast(value, salt):
    return hashlib.sha256(salt + value.encode()).hexdigest()


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "password_hash", fast)
    value = AuthStore(tmp_path / "auth.db")
    value.set_password(PASSWORD, initial=True)
    return value


def at(level):
    return {"id": "req_1", "approval_level": level}


def toolgate(seen, view):
    """ToolGate answers the owner view with `view` (a status code, a body or an exception)."""

    def answer(request):
        seen.append(request)
        if request.method == "GET":
            if isinstance(view, Exception):
                raise view
            if isinstance(view, int):
                return httpx.Response(view, json={"detail": "no"})
            if isinstance(view, str):
                return httpx.Response(200, text=view)
            return httpx.Response(200, json=view)
        if request.url.host == "pi.test":
            return httpx.Response(200, json={"status": "continued"})
        return httpx.Response(200, json={"id": "req_1", "status": "decided"})

    return httpx.Client(transport=httpx.MockTransport(answer))


def apply(client, payload, **options):
    raw = json.dumps(payload).encode()
    return apply_resource(config(), "approvals", io.BytesIO(raw), client, **options)


def decided(seen):
    return [r for r in seen if r.method == "POST" and r.url.path.endswith("/decision")]


UNREADABLE = [
    at(3),
    at(None),
    at("1"),
    at(True),
    at(1.0),
    at(0),
    at(4),
    {"approval_level": 1},  # An older ToolGate or another request's view.
    {"id": "req_2", "approval_level": 1},
    [at(1)],
    "not json",
    404,
    503,
    httpx.ConnectError("down"),
    httpx.ReadTimeout("slow"),
]


@pytest.mark.parametrize("view", UNREADABLE)
def test_a_serious_or_unreadable_yes_is_not_decided_without_the_password(view):
    seen = []
    with toolgate(seen, view) as client, pytest.raises(MutationError, match="password") as refused:
        apply(client, YES)
    assert [request.method for request in seen] == ["GET"]  # it read the level; decided nothing
    assert "pay it" not in str(refused.value)


@pytest.mark.parametrize("level", [1, 2])
def test_a_normal_or_outward_yes_needs_no_password(level):
    seen = []
    with toolgate(seen, at(level)) as client:
        apply(client, YES)
    read, decision, pi = seen
    assert read.method == "GET" and str(read.url) == VIEW
    assert read.headers["x-toolgate-owner-key"] == config().owner_key
    assert decision.method == "POST" and str(decision.url) == VIEW + "/decision"
    assert json.loads(decision.content) == {"status": "approved", "note": "pay it"}
    assert pi.url.path == "/approvals/req_1/decided"


@pytest.mark.parametrize("status", ["rejected", "dismissed"])
def test_a_no_runs_nothing_so_it_needs_no_password_even_when_serious(status):
    seen = []
    with toolgate(seen, at(3)) as client:
        apply(client, {**YES, "status": status})
    # As in the Inbox, it does not even read the level.
    assert [request.method for request in seen] == ["POST", "POST"]
    assert seen[0].url.path == "/v2/owner/requests/req_1/decision"


@pytest.mark.parametrize("view", [at(3), 503])
def test_the_right_password_decides_it_once(auth, view):
    seen = []
    with toolgate(seen, view) as client:
        apply(client, {**YES, "password": PASSWORD}, auth=auth)
    assert [request.method for request in seen] == ["GET", "POST", "POST"]
    assert len(decided(seen)) == 1
    # The password never reaches ToolGate or Pi.
    assert all(PASSWORD.encode() not in request.content for request in seen)
    assert json.loads(decided(seen)[0].content) == {"status": "approved", "note": "pay it"}


@pytest.mark.parametrize(
    "password", ["wrong password, nearly right", "", 7, None, ["x"], {"password": PASSWORD}]
)
def test_anything_but_the_right_password_decides_nothing(auth, password):
    seen = []
    with toolgate(seen, at(3)) as client, pytest.raises(MutationError) as refused:
        apply(client, {**YES, "password": password}, auth=auth)
    assert decided(seen) == []
    message = str(refused.value)
    assert "Nothing was decided" in message
    assert "nearly right" not in message and PASSWORD not in message and "pay it" not in message


def test_a_password_sent_is_checked_even_when_the_level_needs_none(auth):
    seen = []
    with toolgate(seen, at(1)) as client, pytest.raises(MutationError, match="not accepted"):
        apply(client, {**YES, "password": "wrong password, nearly right"}, auth=auth)
    assert decided(seen) == []


def test_a_password_without_a_store_to_check_it_decides_nothing():
    seen = []
    with toolgate(seen, at(3)) as client, pytest.raises(MutationError, match="could not be"):
        apply(client, {**YES, "password": PASSWORD})
    assert decided(seen) == []


def test_wrong_passwords_are_limited_with_the_other_host_commands(auth):
    seen = []
    with toolgate(seen, at(3)) as client:
        for _ in range(5):
            with pytest.raises(MutationError, match="not accepted"):
                apply(client, {**YES, "password": "x" * 20}, auth=auth)
        with pytest.raises(MutationError, match="Wait 5 minutes"):
            apply(client, {**YES, "password": PASSWORD}, auth=auth)
        with pytest.raises(AuthError, match="Wait 5 minutes"):
            auth.host_password(PASSWORD)  # the same budget as spending-cap and owner-preferences
    assert decided(seen) == []


def test_only_a_decision_may_carry_a_password():
    # A valid resume, but with a password: the shape is refused before anything is sent.
    payload = {"operation": "resume", "id": "turn_1234567890abcdef", "password": PASSWORD}
    unused = httpx.MockTransport(lambda _request: pytest.fail("sent"))
    with (
        httpx.Client(transport=unused) as client,
        pytest.raises(MutationError, match="unsupported shape") as refused,
    ):
        apply_resource(config(), "turns", io.BytesIO(json.dumps(payload).encode()), client)
    assert PASSWORD not in str(refused.value)


def test_the_command_exits_3_when_the_yes_needs_the_password(monkeypatch, capsys, tmp_path):
    from gateway import __main__ as entry
    from gateway import api, mutation

    seen = []

    def run(config, resource, stream, client, auth=None):
        seen.append((resource, auth))
        if resource == "approvals":
            raise mutation.PasswordRequired("This yes asks for your password.")
        return {"ok": True}

    monkeypatch.setattr(mutation, "apply_resource", run)
    monkeypatch.setattr(api.Config, "environment", classmethod(lambda cls: config()))
    database = str(tmp_path / "auth.db")
    for resource in ("approvals", "models"):
        monkeypatch.setattr("sys.argv", ["gateway", "apply", resource, "--db", database])
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        if resource == "approvals":
            with pytest.raises(SystemExit) as stopped:
                entry.main()
            assert stopped.value.code == mutation.PASSWORD_REQUIRED_EXIT == 3
        else:
            entry.main()
    [(_, store), (_, none)] = seen
    assert isinstance(store, AuthStore) and none is None
    assert "asks for your password" in capsys.readouterr().err
