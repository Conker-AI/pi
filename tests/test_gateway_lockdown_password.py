"""ToolGate's lockdown comes off only with the owner's password, from the browser and from the host.

ToolGate cannot check the password, so it lifts lockdown only when the gateway's owner key comes
with `X-ToolGate-Owner-Proof: password` (ToolGate's tests/test_owner_lockdown.py). The gateway
sends it only after checking the password: Settings with a proof bound to `{"enabled": false}`,
or `python -m gateway lockdown` with the password on stdin (the host budget). `conker config set`
reaches the same ToolGate route through a relay with the owner key and no proof, and is refused.
"""

import hashlib
import io
import json

import httpx
import pytest
from test_gateway_tool_settings import PASSWORD, gate, proof  # noqa: F401

from gateway import store as module
from gateway.store import AuthError, AuthStore

PROOF = "x-toolgate-owner-proof"
OWNER = "gateway-lockdown-owner-key-" + "o" * 32
OFF = {"lockdown": False, "reason": "", "changed_by": "owner"}


def test_the_settings_switch_sends_the_proof_only_after_the_password(gate):
    client, headers, seen, reply = gate
    reply["answer"] = lambda request: httpx.Response(200, json=OFF)
    path = "/api/owner/lockdown"
    assert client.post(path, json={"enabled": True}, headers=headers).status_code == 200
    assert PROOF not in seen[-1].headers  # turning it on only takes power away
    assert client.post(path, json={"enabled": False}, headers=headers).status_code == 428
    assert len(seen) == 1
    checked = proof(client, headers, "POST", path, {"enabled": False})
    assert client.post(path, json={"enabled": False}, headers=checked).status_code == 200
    assert seen[-1].headers[PROOF] == "password"
    assert json.loads(seen[-1].content) == {"enabled": False}
    client.get(path)
    assert PROOF not in seen[-1].headers


def test_a_proof_header_the_browser_sends_itself_never_reaches_toolgate(gate):
    client, headers, seen, reply = gate
    reply["answer"] = lambda request: httpx.Response(200, json=OFF)
    forged = {**headers, "X-ToolGate-Owner-Proof": "password"}
    path = "/api/owner/lockdown"
    assert client.post(path, json={"enabled": False}, headers=forged).status_code == 428
    assert seen == []
    assert client.post(path, json={"enabled": True}, headers=forged).status_code == 200
    assert PROOF not in seen[-1].headers


def test_toolgate_refusing_it_reaches_the_browser_as_is(gate):
    client, headers, _seen, reply = gate
    refused = {"detail": {"code": "password_required", "message": "Needs the password."}}
    reply["answer"] = lambda request: httpx.Response(403, json=refused)
    checked = proof(client, headers, "POST", "/api/owner/lockdown", {"enabled": False})
    response = client.post("/api/owner/lockdown", json={"enabled": False}, headers=checked)
    assert response.status_code == 403 and response.json() == refused


# python -m gateway lockdown


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "password_hash", lambda v, s: hashlib.sha256(s + v.encode()).hexdigest()
    )
    value = AuthStore(tmp_path / "auth.db")
    value.set_password(PASSWORD, initial=True)
    return value


class Config:
    toolgate_url = "http://toolgate.test"
    owner_key = OWNER


def toolgate(seen, answer=None):
    def handle(request):
        seen.append(request)
        if answer is not None:
            return answer(request)
        return httpx.Response(200, json={**OFF, "lockdown": json.loads(request.content)["enabled"]})

    return httpx.Client(transport=httpx.MockTransport(handle))


def host(auth, client, payload, config=None) -> tuple[int, str, str]:
    from gateway import lockdown

    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    code = lockdown.run(config or Config(), auth, client, io.BytesIO(raw), out, err)
    return code, out.getvalue(), err.getvalue()


def test_the_right_password_lifts_it_with_the_proof(auth):
    seen = []
    with toolgate(seen) as client:
        code, out, err = host(auth, client, {"password": PASSWORD, "enabled": False})
    assert (code, err) == (0, "")
    assert json.loads(out) == OFF
    [sent] = seen
    assert (sent.method, str(sent.url)) == ("POST", "http://toolgate.test/v2/owner/lockdown")
    assert sent.headers["x-toolgate-owner-key"] == OWNER and sent.headers[PROOF] == "password"
    assert json.loads(sent.content) == {"enabled": False}  # never the password
    assert PASSWORD.encode() not in sent.content


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "wrong password, nearly right", "enabled": False},
        {"password": "", "enabled": False},
        {"password": 7, "enabled": False},
        {"enabled": False},
        {"password": PASSWORD},
        {"password": PASSWORD, "enabled": "false"},
        {"password": PASSWORD, "enabled": 0},
        {"password": PASSWORD, "enabled": None},
        {"password": PASSWORD, "enabled": False, "reason": "x"},
        [PASSWORD, False],
        b"\xff\xfe",
        b'{"password": "correct horse battery staple", "enabled": fal',
        b"{" + b" " * 5000 + b"}",
    ],
)
def test_anything_else_changes_nothing_and_is_not_repeated(auth, payload):
    seen = []
    with toolgate(seen) as client:
        code, out, err = host(auth, client, payload)
    assert (code, out, seen) == (1, "", [])
    assert "Nothing was changed" in err
    assert "nearly right" not in err and PASSWORD not in err


def test_wrong_passwords_share_the_host_budget(auth):
    seen = []
    with toolgate(seen) as client:
        for _ in range(5):
            assert host(auth, client, {"password": "x" * 20, "enabled": False})[0] == 1
        code, _, err = host(auth, client, {"password": PASSWORD, "enabled": False})
    assert code == 1 and "Wait 5 minutes" in err and seen == []
    with pytest.raises(AuthError, match="Wait 5 minutes"):
        auth.host_password(PASSWORD)


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (httpx.ConnectError("refused"), "ToolGate did not answer. Nothing was changed."),
        (httpx.ReadTimeout("no answer"), "It may already be off"),
        (httpx.Response(403, json={"detail": {"message": PASSWORD}}), "HTTP 403"),
        (httpx.Response(200, text="not json"), "It may already be off"),
        (httpx.Response(200, json={**OFF, "lockdown": True}), "ToolGate did not confirm"),
    ],
    ids=["offline", "lost", "refused", "unreadable", "unconfirmed"],
)
def test_toolgate_trouble_is_reported_without_the_body(auth, answer, said):
    def reply(request):
        if isinstance(answer, Exception):
            raise answer
        return answer

    with toolgate([], reply) as client:
        code, out, err = host(auth, client, {"password": PASSWORD, "enabled": False})
    assert (code, out) == (1, "") and said in err and PASSWORD not in err


def test_without_the_owner_channel_nothing_is_sent_or_checked(auth):
    class Missing(Config):
        owner_key = ""

    seen = []
    with toolgate(seen) as client:
        code, _, err = host(
            auth, client, {"password": "wrong but unused", "enabled": False}, Missing()
        )
    assert code == 1 and "not configured" in err and seen == []


def test_the_command_is_wired_and_reads_stdin(monkeypatch):
    from gateway import __main__ as entry
    from gateway import lockdown

    seen = []
    monkeypatch.setattr(lockdown, "main", lambda db: seen.append(db) or 0)
    monkeypatch.setattr("sys.argv", ["gateway", "lockdown", "--db", "/tmp/auth.db"])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 0 and seen == ["/tmp/auth.db"]
