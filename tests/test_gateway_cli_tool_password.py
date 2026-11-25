"""`conker apply tools` asks for the owner's password where Tools does (gateway/owner_routes.py).

In the browser, letting chats run a published tool (`access` on) and publishing one to run without
asking (`authorization: auto`) or for Conker to use (`conkerMayUse`) need a password proof bound to
that exact body; saving a draft, withdrawing access, publishing to ask first each time and a run by
hand need only the signed-in session. The host command used to send every one of them with the
gateway's ToolGate keys and no password. It now refuses those three, sending nothing, until the
owner's password comes with the change on stdin (exit 3 without it, as `apply approvals`).
"""

import hashlib
import io
import json

import httpx
import pytest

from gateway import store as module
from gateway.api import Config
from gateway.mutation import MutationError, PasswordRequired, apply_resource
from gateway.store import AuthError, AuthStore

PASSWORD = "correct horse battery staple"
DIGEST = "a" * 64
GRANT = {"operation": "access", "id": "weekly", "version": 2, "digest": DIGEST, "enabled": True}
PUBLISH = {
    "operation": "publish",
    "id": "weekly",
    "expected_revision": 3,
    "expected_publication_version": 1,
    "authorization": "owner_confirmation",
}
WIDENING = {
    "access": GRANT,
    "auto": {**PUBLISH, "authorization": "auto"},
    "conker": {**PUBLISH, "conkerMayUse": True},
    "auto-conker": {**PUBLISH, "authorization": "auto", "conkerMayUse": True},
}
SENT_TO = {
    "access": "/v2/owner/editor-drafts/weekly/access",
    "auto": "/v2/owner/editor-drafts/weekly/publish",
    "conker": "/v2/owner/editor-drafts/weekly/publish",
    "auto-conker": "/v2/owner/editor-drafts/weekly/publish",
}
SESSION_ONLY = {
    "withdraw": {**GRANT, "enabled": False},
    "ask-first": PUBLISH,
    "ask-first-not-for-conker": {**PUBLISH, "conkerMayUse": False},
    "save": {
        "operation": "save",
        "id": "weekly",
        "expected_revision": 0,
        "document": {"id": "weekly"},
    },
    "run": {
        "operation": "run",
        "id": "weekly",
        "version": 2,
        "digest": DIGEST,
        "action_id": "editor_" + "b" * 32,
    },
    "run-tool": {"operation": "run_tool", "id": "web.search", "action_id": "owner_" + "c" * 32},
}


def config() -> Config:
    return Config(
        "https://conker.test",
        "/auth/auth.db",
        "http://pi.test",
        "runtime-" + "r" * 32,
        "http://toolgate.test",
        "owner-" + "o" * 32,
        toolgate_execution_key="execution-" + "e" * 32,
    )


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "password_hash", lambda v, s: hashlib.sha256(s + v.encode()).hexdigest()
    )
    value = AuthStore(tmp_path / "auth.db")
    value.set_password(PASSWORD, initial=True)
    return value


@pytest.fixture
def toolgate():
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "weekly", "status": "done"})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        yield client, seen


def apply(client, payload, auth):
    raw = json.dumps(payload).encode()
    return apply_resource(config(), "tools", io.BytesIO(raw), client, auth)


@pytest.mark.parametrize("change", WIDENING, ids=list(WIDENING))
def test_a_grant_or_a_publish_that_runs_unasked_is_not_sent_without_the_password(
    toolgate, auth, change
):
    client, seen = toolgate
    with pytest.raises(PasswordRequired, match="asks for your password, as Tools does"):
        apply(client, WIDENING[change], auth)
    assert seen == []


@pytest.mark.parametrize("change", WIDENING, ids=list(WIDENING))
def test_with_the_right_password_the_same_change_is_sent_once(toolgate, auth, change):
    client, seen = toolgate
    assert apply(client, {**WIDENING[change], "password": PASSWORD}, auth)["status"] == "done"
    [sent] = seen
    assert (sent.method, sent.url.path) == ("POST", SENT_TO[change])
    assert PASSWORD.encode() not in sent.content and b"password" not in sent.content


@pytest.mark.parametrize(
    "password",
    ["wrong password, nearly right", "", 7, None, ["x"]],
    ids=["wrong", "empty", "number", "null", "list"],
)
def test_a_wrong_or_malformed_password_sends_nothing_and_is_not_repeated(toolgate, auth, password):
    client, seen = toolgate
    with pytest.raises(MutationError) as refused:
        apply(client, {**GRANT, "password": password}, auth)
    assert "Nothing was changed" in str(refused.value)
    assert "nearly right" not in str(refused.value)
    assert seen == []


@pytest.mark.parametrize("change", SESSION_ONLY, ids=list(SESSION_ONLY))
def test_what_tools_does_with_the_session_alone_needs_no_password(toolgate, auth, change):
    client, seen = toolgate
    apply(client, SESSION_ONLY[change], None)
    assert len(seen) == 1 and b"password" not in seen[0].content


def test_a_password_sent_is_checked_even_when_the_change_needs_none(toolgate, auth):
    client, seen = toolgate
    with pytest.raises(MutationError, match="Password not accepted"):
        apply(client, {**SESSION_ONLY["withdraw"], "password": "wrong password here"}, auth)
    assert seen == []


def test_wrong_passwords_share_the_host_budget(toolgate, auth):
    client, seen = toolgate
    for _ in range(5):
        with pytest.raises(MutationError):
            apply(client, {**GRANT, "password": "x" * 20}, auth)
    with pytest.raises(MutationError, match="Wait 5 minutes"):
        apply(client, {**GRANT, "password": PASSWORD}, auth)
    with pytest.raises(AuthError, match="Wait 5 minutes"):
        auth.host_password(PASSWORD)  # spending-cap, owner-preferences and approvals wait too
    assert seen == []


def test_without_the_password_store_nothing_is_sent(toolgate):
    client, seen = toolgate
    with pytest.raises(MutationError, match="could not be checked"):
        apply(client, {**GRANT, "password": PASSWORD}, None)
    assert seen == []


def test_the_command_checks_tool_changes_and_exits_3_without_the_password(monkeypatch, tmp_path):
    from gateway import __main__ as entry
    from gateway import mutation

    stores = []

    def refuse(config, resource, stream, client, auth=None):
        stores.append((resource, type(auth).__name__))
        raise mutation.PasswordRequired("needs it")

    monkeypatch.setattr(mutation, "apply_resource", refuse)
    monkeypatch.setattr(Config, "environment", staticmethod(config))
    monkeypatch.setattr(Config, "validate", lambda self: None)
    monkeypatch.setattr("sys.argv", ["gateway", "apply", "tools", "--db", str(tmp_path / "a.db")])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 3
    assert stores == [("tools", "AuthStore")]
