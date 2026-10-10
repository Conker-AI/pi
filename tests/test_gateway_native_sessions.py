"""Native session reads never become inference or unverified process-control authority."""

import hashlib
import json
import sqlite3
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import native_sessions as native
from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.operations import fingerprint
from gateway.store import AuthError, AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic owner password for native sessions"
PREFIX = "/api/host/claude/sessions"
ID = "native_session-A"
IDENTITY = "a" * 64
BODY = {"expectedIdentity": IDENTITY}


def row(**changes):
    return {
        "id": ID,
        "identity": IDENTITY,
        "sessionId": "official-session",
        "name": "Synthetic native work",
        "cwd": "C:/synthetic/project",
        "startedAt": 1234567890000,
        "state": "working",
        "processStatus": "busy",
        "waitingFor": None,
        "canStop": True,
        **changes,
    }


def payload(action, **changes):
    common = {"schemaVersion": 1, "provider": "claude"}
    values = {
        "list": {
            "scope": "background_sessions",
            "results": [row()],
            "truncated": False,
            "omittedCount": 0,
        },
        "status": {"result": row()},
        "output": {
            "id": ID,
            "identity": IDENTITY,
            "text": "Synthetic private output",
            "truncated": False,
            "outputPersisted": False,
        },
        "stop": {
            "id": ID,
            "identity": IDENTITY,
            "status": "stop_requested",
            "automaticReplay": False,
        },
    }
    return {**common, **values[action], **changes}


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen, response = [], [None]

    def host(request):
        seen.append(request)
        if isinstance(response[0], Exception):
            raise response[0]
        if response[0] is not None:
            return response[0]
        action = request.url.path.rsplit("/", 1)[-1]
        action = (
            "list" if action == "sessions" else action if action in {"output", "stop"} else "status"
        )
        return httpx.Response(200, json=payload(action))

    app = create_app(
        Config(ORIGIN, str(auth.path), "http://pi", "r" * 32),
        store=auth,
        provider_transport=httpx.MockTransport(host),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get(PREFIX, headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
        csrf = client.get("/auth/session").json()["csrf_token"]
        logged = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": logged.json()["csrf_token"]}
        yield client, headers, seen, response, auth


def proof(client, headers, path, body=BODY):
    result = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": body}},
    )
    assert result.status_code == 200, result.text
    return {**headers, "X-Conker-Verification": result.json()["verification_token"]}


def test_owner_only_exact_reads_do_not_launch_or_persist_output(boundary):
    client, _, seen, _, auth = boundary
    for suffix, action in [("", "list"), (f"/{ID}", "status"), (f"/{ID}/output", "output")]:
        result = client.get(PREFIX + suffix)
        assert result.status_code == 200
        assert result.json() == payload(action)
        assert "no-store" in result.headers["cache-control"]
    assert [request.url.path for request in seen] == [
        "/claude/sessions",
        f"/claude/sessions/{ID}",
        f"/claude/sessions/{ID}/output",
    ]
    for request in seen:
        assert request.method == "GET" and not request.content
        assert set(request.extensions["timeout"].values()) == {30}
        assert all(
            name not in request.headers
            for name in ["cookie", "x-conker-verification", "x-pi-gateway-key", "x-csrf-token"]
        )
    with closing(sqlite3.connect(auth.path)) as db:
        assert "Synthetic private output" not in "\n".join(db.iterdump())


def test_stop_requires_csrf_exact_id_identity_and_single_use_proof(boundary):
    client, headers, seen, _, _ = boundary
    path = f"{PREFIX}/{ID}/stop"
    assert client.post(path, json=BODY).status_code == 403
    assert (
        client.post(
            path, json=BODY, headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert client.post(path, json=BODY, headers=headers).status_code == 428
    checked = proof(client, headers, path)
    assert client.post(f"{PREFIX}/different/stop", json=BODY, headers=checked).status_code == 428
    assert (
        client.post(path, json={"expectedIdentity": "b" * 64}, headers=checked).status_code == 428
    )
    assert not seen
    result = client.post(path, json=BODY, headers=checked)
    assert result.status_code == 200 and result.json() == payload("stop")
    assert client.post(path, json=BODY, headers=checked).status_code == 428
    assert len(seen) == 1
    assert seen[0].url.path == f"/claude/sessions/{ID}/stop"
    assert json.loads(seen[0].content) == BODY
    assert set(seen[0].extensions["timeout"].values()) == {30}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expectedIdentity": "A" * 64},
        {**BODY, "force": True},
        {"expectedIdentity": 1},
        {"expectedIdentity": None},
        {**BODY, "permissionMode": "bypassPermissions"},
    ],
)
def test_invalid_stop_cannot_mint_authority_or_reach_host(boundary, body):
    client, headers, seen, _, _ = boundary
    path = f"{PREFIX}/{ID}/stop"
    assert client.post(path, json=body, headers=headers).status_code == 422
    with pytest.raises(AuthError):
        fingerprint("POST", path, body)
    assert not seen


def test_no_queries_or_unsupported_native_controls(boundary):
    client, headers, seen, _, _ = boundary
    for suffix in ["", f"/{ID}", f"/{ID}/output"]:
        assert client.get(PREFIX + suffix + "?all=true").status_code == 422
    assert client.get(PREFIX + "/bad.id").status_code == 422
    assert client.get(PREFIX + "/" + "a" * 101).status_code == 422
    assert (
        client.post(f"{PREFIX}/{ID}/stop?force=true", json=BODY, headers=headers).status_code == 422
    )
    for action in ["launch", "approve", "resume", "permissions"]:
        path = f"{PREFIX}/{ID}/{action}"
        assert client.post(path, json=BODY, headers=headers).status_code in {404, 405}
        with pytest.raises(AuthError):
            fingerprint("POST", path, BODY)
    assert not seen


@pytest.mark.parametrize("code", list(native.ERRORS))
def test_fixed_host_errors_are_preserved_without_raw_content(boundary, code):
    client, _, seen, response, _ = boundary
    response[0] = httpx.Response(native.ERRORS[code][0], json={"error": code})
    result = client.get(f"{PREFIX}/{ID}")
    assert result.status_code == native.ERRORS[code][0]
    assert result.json()["detail"]["code"] == code
    assert len(seen) == 1


def test_timeout_unknown_stop_is_not_replayed_and_consumes_proof(boundary):
    client, headers, seen, response, _ = boundary
    response[0] = httpx.ReadTimeout("host-private-password")
    path = f"{PREFIX}/{ID}/stop"
    checked = proof(client, headers, path)
    result = client.post(path, json=BODY, headers=checked)
    assert result.status_code == 503
    assert result.json()["detail"]["code"] == "native_sessions_unavailable"
    assert "host-private-password" not in result.text
    assert client.post(path, json=BODY, headers=checked).status_code == 428
    assert len(seen) == 1


@pytest.mark.parametrize(
    "action,changes",
    [
        ("list", {"results": [row(), row()]}),
        ("list", {"results": [row(id=f"session-{i}") for i in range(101)]}),
        ("list", {"results": [row(state="unknown")]}),
        ("list", {"results": [row(startedAt=True)]}),
        ("list", {"schemaVersion": True}),
        ("status", {"result": row(id="replacement")}),
        ("status", {"result": row(sessionId="unsafe/id")}),
        ("status", {"result": row(identity="A" * 64)}),
        ("status", {"result": row(name="Unsafe\x1b[31mname")}),
        ("status", {"result": row(cwd="C:/project\u202eevil")}),
        ("output", {"id": "replacement"}),
        ("output", {"identity": "invalid"}),
        ("output", {"text": "😀" * 16385}),
        ("output", {"outputPersisted": True}),
        ("output", {"outputPersisted": 0}),
        ("output", {"text": "Unexpected\x1b[31mcontrol"}),
        ("output", {"text": "Hidden\u200bformat"}),
        ("stop", {"identity": "b" * 64}),
        ("stop", {"status": "stopped"}),
        ("stop", {"automaticReplay": True}),
    ],
)
def test_malformed_or_mismatched_host_evidence_fails_closed(action, changes):
    value = payload(action, **changes)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=value))
    with pytest.raises(native.NativeSessionsError, match="unavailable"):
        native.request(
            transport, action, None if action == "list" else ID, BODY if action == "stop" else None
        )


def test_output_utf8_boundary_and_truthful_empty_or_unknown():
    for action, value in [
        ("output", payload("output", text="😀" * 16384)),
        ("output", payload("output", text="plain\tline\r\n日本語")),
        ("list", payload("list", results=[])),
        ("status", payload("status", result=row(state="unknown", canStop=False))),
    ]:
        transport = httpx.MockTransport(lambda _, value=value: httpx.Response(200, json=value))
        assert native.request(transport, action, None if action == "list" else ID) == value
    with pytest.raises(native.NativeSessionsError) as error:
        native.request(None, "list")
    assert error.value.status == 503


@pytest.mark.parametrize(
    "value",
    [
        payload("list", results=[], omittedCount=1),
        payload("list", results=[row(id=f"session-{i}") for i in range(100)], truncated=True),
    ],
)
def test_non_background_omissions_are_independent_of_background_truncation(value):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=value))
    assert native.request(transport, "list") == value


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://external.invalid"}),
        httpx.Response(500, json={"error": "secret-host-message"}),
        httpx.Response(409, json={"error": "native_session_changed", "secret": "host-secret"}),
        httpx.Response(200, content=b'{"schemaVersion":1,"schemaVersion":1}'),
        httpx.Response(200, content=b'{"value":NaN}'),
    ],
)
def test_redirects_errors_and_ambiguous_json_are_static_failures(response):
    seen = []

    def host(request):
        seen.append(request)
        return response

    with pytest.raises(native.NativeSessionsError) as error:
        native.request(httpx.MockTransport(host), "list")
    assert error.value.code == "native_sessions_unavailable" and len(seen) == 1
    assert "secret" not in str(error.value)


def test_stream_cap_closes_transport_without_buffering_unbounded_output():
    class Stream(httpx.SyncByteStream):
        chunks = 0
        closed = False

        def __iter__(self):
            for _ in range(1000):
                self.chunks += 1
                yield b"x" * 8192

        def close(self):
            self.closed = True

    stream = Stream()
    with pytest.raises(native.NativeSessionsError):
        native.request(
            httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)), "output", ID
        )
    assert stream.closed and stream.chunks <= 65
