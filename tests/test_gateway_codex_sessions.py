"""Codex discovery carries bounded stored metadata, never native execution authority."""

import hashlib
import json
import sqlite3
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import codex_sessions as native
from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.native_sessions import NativeSessionsError
from gateway.store import AuthStore
from pi.models.private_transport import PrivateTransport
from pi.owner.native_codex_query import canonical_query, parse_query

ORIGIN = "https://localhost:8050"
PREFIX = "/api/host/codex/sessions"
ID = "01234567-89ab-cdef-0123-456789abcdef"
OTHER = "11234567-89ab-cdef-0123-456789abcdef"
PASSWORD = "Synthetic Codex discovery owner password"


def row(**changes):
    return {
        "id": ID,
        "name": "Synthetic private title",
        "preview": "Synthetic preview, never a transcript",
        "previewTruncated": False,
        "cwd": "C:/synthetic/project",
        "createdAt": 1700000000,
        "updatedAt": 1700000001,
        "modelProvider": "openai",
        "source": "cli",
        **changes,
    }


def payload(action="list", **changes):
    return {
        "schemaVersion": 1,
        "provider": "codex",
        "scope": "saved_threads",
        "runtimeVisibility": "observer_only",
        **(
            {"archived": False, "results": [row()], "nextCursor": None}
            if action == "list"
            else {"session": row()}
        ),
        **changes,
    }


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
        return httpx.Response(
            200,
            json=(
                payload(archived=request.url.params.get("archived") == "true")
                if request.url.path == "/codex/sessions"
                else payload("status")
            ),
        )

    transport = PrivateTransport(
        "http://127.0.0.1:18570", "private-control-key-" * 3, transport=httpx.MockTransport(host)
    )
    app = create_app(
        Config(ORIGIN, str(auth.path), "http://pi", "r" * 32),
        store=auth,
        provider_transport=transport,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, seen, response, auth


def login(client):
    csrf = client.get("/auth/session").json()["csrf_token"]
    result = client.post(
        "/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert result.status_code == 200


def test_owner_reads_forward_only_bounded_metadata_query_and_never_persist_it(boundary):
    client, seen, _, auth = boundary
    assert client.get(PREFIX).status_code == 401
    assert client.get(PREFIX, headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
    assert (
        client.get(PREFIX, headers={"Authorization": "Bearer " + "inference" * 5}).status_code
        == 401
    )
    assert not seen
    login(client)
    for suffix, expected in [
        ("", payload()),
        ("?limit=50&archived=true&cursor=opaque_CURSOR-1", payload(archived=True)),
        (f"/{ID}", payload("status")),
    ]:
        result = client.get(PREFIX + suffix)
        assert result.status_code == 200 and result.json() == expected
        assert result.headers["cache-control"] == "no-store"
    assert [str(request.url) for request in seen] == [
        "http://127.0.0.1:18570/codex/sessions?limit=25&archived=false",
        "http://127.0.0.1:18570/codex/sessions?limit=50&archived=true&cursor=opaque_CURSOR-1",
        f"http://127.0.0.1:18570/codex/sessions/{ID}",
    ]
    for request in seen:
        assert request.method == "GET" and not request.content
        assert request.headers["authorization"] == "Bearer " + "private-control-key-" * 3
        assert request.headers["host"] == "127.0.0.1:18570"
        assert set(request.extensions["timeout"].values()) == {30}
        assert all(
            name not in request.headers
            for name in ["cookie", "x-conker-verification", "x-csrf-token", "x-pi-gateway-key"]
        )
    with closing(sqlite3.connect(auth.path)) as db:
        assert "Synthetic private title" not in "\n".join(db.iterdump())


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=01",
        "limit=51",
        "limit=true",
        "limit=1.0",
        "limit=",
        "limit=1&limit=2",
        "archived=",
        "archived=1",
        "archived=False",
        "archived=true&archived=false",
        "cursor=",
        "cursor=a%3Db",
        "cursor=a%0Ab",
        "cursor=%FF",
        "cursor=%xx",
        "cursor=" + "a" * 8193,
        "cursor=a&cursor=b",
        "q=private",
        "scope=all",
        "limit=2&archived=false&cursor=a&unknown=x",
        "limit",
        "limit=2&&archived=false",
    ],
)
def test_invalid_list_queries_are_refused_before_transport(boundary, query):
    client, seen, _, _ = boundary
    login(client)
    result = client.get(PREFIX + "?" + query)
    assert result.status_code == 422
    assert result.json()["detail"]["code"] == "invalid_native_session_request"
    assert not seen


@pytest.mark.parametrize("identity", ["wrong", ID.upper(), ID + "\n", "../status"])
def test_invalid_thread_id_has_no_host_read(identity):
    seen = []
    with pytest.raises(NativeSessionsError) as exc:
        native.request(
            httpx.MockTransport(lambda request: seen.append(request)), "status", identity
        )
    assert exc.value.status == 422 and not seen


def test_no_detail_query_transcript_or_mutation_route(boundary):
    client, seen, _, _ = boundary
    login(client)
    assert client.get(f"{PREFIX}/{ID}?includeTurns=true").status_code == 422
    for suffix in ["", f"/{ID}", f"/{ID}/stop", f"/{ID}/resume", f"/{ID}/output"]:
        assert client.post(PREFIX + suffix, json={}).status_code in {404, 405}
    assert client.get(f"{PREFIX}/{ID}/output").status_code == 404
    assert not seen


@pytest.mark.parametrize(
    "action,changes,query",
    [
        ("list", {"schemaVersion": True}, b""),
        ("list", {"runtimeVisibility": "live"}, b""),
        ("list", {"archived": 0}, b""),
        ("list", {"archived": True}, b""),
        ("list", {"results": [row(), row()]}, b""),
        ("list", {"results": [row(), row(id=OTHER)]}, b"limit=1"),
        ("list", {"nextCursor": "bad\n"}, b""),
        ("list", {"nextCursor": "a" * 8193}, b""),
        ("list", {"nextCursor": "same"}, b"cursor=same"),
        ("status", {"session": row(id=OTHER)}, b""),
        ("status", {"session": row(id=ID.upper())}, b""),
        ("status", {"session": row(createdAt=True)}, b""),
        ("status", {"session": row(updatedAt=9007199254740992)}, b""),
        ("status", {"session": row(name="x" * 301)}, b""),
        ("status", {"session": row(preview="x" * 501)}, b""),
        ("status", {"session": row(previewTruncated=0)}, b""),
        ("status", {"session": row(cwd="")}, b""),
        ("status", {"session": row(modelProvider="x" * 201)}, b""),
        ("status", {"session": row(source="other")}, b""),
        ("status", {"session": row(name="x\x1by")}, b""),
        ("status", {"session": row(preview="x\u200by")}, b""),
        ("status", {"session": row(cwd="x\u202ey")}, b""),
        ("status", {"session": row(preview="x\ud800y")}, b""),
        ("status", {"session": row(turns=[{"text": "private transcript"}])}, b""),
        ("status", {"archived": False}, b""),
    ],
)
def test_invalid_host_dto_fails_closed(action, changes, query):
    raw = json.dumps(payload(action, **changes)).encode()
    with pytest.raises(NativeSessionsError) as exc:
        native.request(
            httpx.MockTransport(lambda _: httpx.Response(200, content=raw)),
            action,
            None if action == "list" else ID,
            query,
        )
    assert exc.value.code == "native_sessions_unavailable"


def test_exact_bounds_empty_history_and_missing_host_are_distinct():
    assert parse_query(b"") == {"limit": 25, "archived": False}
    assert (
        canonical_query(b"cursor=A_%2D&archived=true&limit=50")
        == b"limit=50&archived=true&cursor=A_-"
    )
    for value in [
        payload(results=[]),
        payload(nextCursor="a" * 8192),
        payload("status", session=row(name=None, preview="😀" * 500, cwd="x" * 4096)),
    ]:
        action = "list" if "results" in value else "status"
        assert (
            native.request(
                httpx.MockTransport(lambda _, value=value: httpx.Response(200, json=value)),
                action,
                None if action == "list" else ID,
            )
            == value
        )
    with pytest.raises(NativeSessionsError) as exc:
        native.request(None, "list")
    assert exc.value.status == 503


def test_full_page_at_unicode_metadata_bounds_fits_wire_cap():
    value = payload(
        results=[
            row(
                id=f"{index:08x}-89ab-cdef-0123-456789abcdef",
                name="😀" * 300,
                preview="😀" * 500,
                cwd="😀" * 4096,
                modelProvider="😀" * 200,
            )
            for index in range(50)
        ],
        nextCursor="a" * 8192,
    )
    response = httpx.Response(200, json=value)
    assert len(response.content) < native.CAPS["list"]
    assert (
        native.request(httpx.MockTransport(lambda _: response), "list", query=b"limit=50") == value
    )


@pytest.mark.parametrize("raw", [b"x" * 25001, b"cursor=\xff", b"cursor=%", b"cursor=%0"])
def test_query_parser_bounds_raw_bytes_and_rejects_invalid_encoding(raw):
    with pytest.raises(ValueError):
        parse_query(raw)


def test_once_encoded_cursor_is_canonicalized_before_smaller_host_query_cap():
    raw = b"limit=50&archived=true&cursor=" + b"%61" * 8192
    assert len(raw) > 8500
    forwarded = canonical_query(raw)
    assert forwarded == b"limit=50&archived=true&cursor=" + b"a" * 8192
    assert len(forwarded) < 8300


@pytest.mark.parametrize(
    "status,body,code",
    [
        (422, {"error": "invalid_native_session_request"}, "invalid_native_session_request"),
        (404, {"error": "native_session_not_found"}, "native_session_not_found"),
        (503, {"error": "native_sessions_unavailable"}, "native_sessions_unavailable"),
        (409, {"error": "native_session_changed"}, "native_sessions_unavailable"),
        (500, {"error": "private host error"}, "native_sessions_unavailable"),
        (
            404,
            {"error": "native_session_not_found", "secret": "private"},
            "native_sessions_unavailable",
        ),
        (503, ["private"], "native_sessions_unavailable"),
    ],
)
def test_host_errors_are_fixed_and_not_replayed(boundary, status, body, code):
    client, seen, response, _ = boundary
    login(client)
    response[0] = httpx.Response(status, json=body)
    result = client.get(PREFIX)
    assert result.json()["detail"]["code"] == code and len(seen) == 1
    assert "private" not in result.text


@pytest.mark.parametrize(
    "response",
    [
        httpx.ReadTimeout("private host secret"),
        httpx.Response(302, headers={"Location": "https://external.invalid"}),
        httpx.Response(200, content=b'{"schemaVersion":1,"schemaVersion":1}'),
        httpx.Response(200, content=b'{"value":NaN}'),
    ],
)
def test_unknown_read_outcomes_are_static_and_never_replayed(boundary, response):
    client, seen, configured, _ = boundary
    login(client)
    configured[0] = response
    result = client.get(PREFIX)
    assert result.status_code == 503 and "private" not in result.text and len(seen) == 1


def test_stream_cap_stops_and_closes_before_unbounded_metadata_is_buffered():
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
    with pytest.raises(NativeSessionsError):
        native.request(httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)), "list")
    assert stream.closed and stream.chunks <= 257


@pytest.mark.parametrize(
    "method,url,prefix",
    [
        ("POST", "http://conker-host/codex/sessions?limit=2", ""),
        ("GET", "http://conker-host/chatgpt?limit=2", ""),
        ("GET", "http://conker-host/claude/sessions?limit=2", ""),
        ("GET", f"http://conker-host/codex/sessions/{ID}?limit=2", ""),
        ("GET", "http://conker-host/codex/%73essions?limit=2", ""),
        ("GET", "http://conker-chatgpt/codex/sessions?limit=2", ""),
        ("GET", "http://conker-host/codex/sessions?limit=2", "/inference"),
        ("GET", "https://conker-host/codex/sessions?limit=2", ""),
        ("GET", "http://user@conker-host/codex/sessions?limit=2", ""),
        ("GET", "http://conker-host/codex/sessions?limit=2#fragment", ""),
        ("GET", "http://conker-host/codex/sessions?cursor=bad%0A", ""),
    ],
)
def test_private_query_exception_does_not_widen_other_destinations(method, url, prefix):
    seen = []
    transport = PrivateTransport(
        "http://127.0.0.1:18570",
        "k" * 32,
        prefix=prefix,
        transport=httpx.MockTransport(lambda request: seen.append(request)),
    )
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError):
        client.request(method, url)
    assert not seen
