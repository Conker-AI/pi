"""`python -m gateway check-password`: the owner's password checked for a change made at the host.

Some host changes reach no service that could ask for the password: `conker providers spending
on|off|recover` writes the paid-model policy into the host's own files and restarts Pi. Settings
asks the password for it (the gateway's `/api/host/providers`, `paid-policy`). The CLI asks for it
too and has it checked here first, with the host budget (five wrong per five minutes, shared with
`spending-cap`, `owner-preferences`, `lockdown` and `apply`), and changes nothing unless it passed.
"""

import hashlib
import io
import json

import pytest

from gateway import store as module
from gateway.store import AuthError, AuthStore

PASSWORD = "correct horse battery staple"


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "password_hash", lambda v, s: hashlib.sha256(s + v.encode()).hexdigest()
    )
    value = AuthStore(tmp_path / "auth.db")
    value.set_password(PASSWORD, initial=True)
    return value


def check(auth, payload) -> tuple[int, str, str]:
    from gateway import host_check

    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    code = host_check.run(auth, io.BytesIO(raw), out, err)
    return code, out.getvalue(), err.getvalue()


def test_the_right_password_passes(auth):
    code, out, err = check(auth, {"password": PASSWORD})
    assert (code, json.loads(out), err) == (0, {"checked": True}, "")


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "wrong password, nearly right"},
        {"password": ""},
        {"password": 7},
        {"password": None},
        {},
        {"password": PASSWORD, "extra": 1},
        [PASSWORD],
        b"\xff\xfe",
        b'{"password": "correct horse battery staple"',
        b'{"password": "' + b"x" * 5000 + b'"}',
    ],
)
def test_anything_else_fails_without_repeating_it(auth, payload):
    code, out, err = check(auth, payload)
    assert (code, out) == (1, "")
    assert "Nothing was changed" in err
    assert "nearly right" not in err and PASSWORD not in err


def test_wrong_passwords_share_the_host_budget(auth):
    for _ in range(5):
        assert check(auth, {"password": "x" * 20})[0] == 1
    code, out, err = check(auth, {"password": PASSWORD})
    assert (code, out) == (1, "") and "Wait 5 minutes" in err
    with pytest.raises(AuthError, match="Wait 5 minutes"):
        auth.host_password(PASSWORD)


def test_without_a_password_set_it_fails(tmp_path):
    code, _, err = check(AuthStore(tmp_path / "auth.db"), {"password": PASSWORD})
    assert code == 1 and "No password set" in err


def test_the_command_is_wired_and_reads_stdin(monkeypatch):
    from gateway import __main__ as entry
    from gateway import host_check

    seen = []
    monkeypatch.setattr(host_check, "main", lambda db: seen.append(db) or 0)
    monkeypatch.setattr("sys.argv", ["gateway", "check-password", "--db", "/tmp/auth.db"])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 0 and seen == ["/tmp/auth.db"]
