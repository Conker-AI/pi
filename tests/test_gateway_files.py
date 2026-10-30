"""The gateway's file relay: owner downloads out, ChatGPT and Claude imports in (gateway/relay.py)."""

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import file_routes
from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.content.office_exports import MIME

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic file relay owner phrase"
OWNER = "file-relay-owner-key-" + "o" * 32
ARTIFACT = "artifact_" + "a" * 32
ARTIFACT_PATH = f"/api/control/pi/artifacts/{ARTIFACT}/download"
ANSWER_PATH = "/api/control/pi/sessions/ses_" + "1" * 16 + "/messages/msg_" + "2" * 16 + "/download"
EXPORT = json.dumps([{"uuid": "c1", "name": "Essay", "chat_messages": []}]).encode()
DIGEST = hashlib.sha256(EXPORT).hexdigest()


def document(format="docx", **headers):
    return httpx.Response(
        200,
        content=b"PK\x03\x04 document bytes",
        headers={
            "content-type": MIME.get(format, format),
            "content-disposition": f'attachment; filename="Plan-v1.{format}"; '
            f"filename*=UTF-8''%D7%AA%D7%9B%D7%A0%D7%99%D7%AA-v1.{format}",
            "set-cookie": "upstream=secret; Path=/",
            "x-private": "upstream-private-secret",
            **headers,
        },
    )


def streamed(response, **headers):
    """The same answer with its body still to be read, as from a real connection."""

    async def chunks():
        yield response.content

    return httpx.Response(
        response.status_code, content=chunks(), headers={**response.headers, **headers}
    )


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen, reply = [], {"answer": lambda request: document()}

    def upstream(request):
        seen.append(request)
        return reply["answer"](request)

    config = Config(ORIGIN, str(auth.path), "http://pi.test", "r" * 32, pi_owner_key=OWNER)
    mock = httpx.MockTransport(upstream)
    app = create_app(config, store=auth, transport=mock, stream_transport=mock)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        yield client, headers, seen, reply


def proof(client, headers, path, body):
    response = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": body}},
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Conker-Verification": response.json()["verification_token"]}


@pytest.mark.parametrize("format", ["docx", "pdf", "pptx", "xlsx"])
def test_a_download_streams_the_file_with_only_its_type_name_and_size(relay, format):
    client, _, seen, reply = relay
    reply["answer"] = lambda request: document(format)
    response = client.get(ARTIFACT_PATH, params={"format": format})
    assert response.status_code == 200, response.text
    assert response.content == b"PK\x03\x04 document bytes"
    assert response.headers["content-type"] == MIME[format]
    assert response.headers["content-disposition"] == (
        f"attachment; filename=\"Plan-v1.{format}\"; filename*=UTF-8''"
        f"%D7%AA%D7%9B%D7%A0%D7%99%D7%AA-v1.{format}"
    )
    assert response.headers["content-length"] == str(len(response.content))
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers and "x-private" not in response.headers
    [request] = seen
    assert str(request.url) == f"http://pi.test/artifacts/{ARTIFACT}/download?format={format}"
    assert request.headers["x-pi-owner-key"] == OWNER
    assert request.headers["accept-encoding"] == "identity"
    assert "cookie" not in request.headers and "x-pi-gateway-key" not in request.headers


@pytest.mark.parametrize(
    "format,media,sent", [("csv", "text/csv", "text/csv"), ("json", "application/json", "json")]
)
def test_csv_and_json_are_an_artifacts_native_export(relay, format, media, sent):
    client, _, seen, reply = relay
    reply["answer"] = lambda request: httpx.Response(
        200,
        content=b'"a","b"',
        headers={
            "content-type": media + ";charset=utf-8",
            "content-disposition": f'attachment; filename="Table-v2.{format}"',
        },
    )
    response = client.get(ARTIFACT_PATH, params={"format": format})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == media + "; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="Table-v2.{format}"'
    assert seen[0].url.params["format"] == "native"
    # Another kind's native file (here a markdown artifact's text) is not what was asked for.
    reply["answer"] = lambda request: httpx.Response(
        200,
        content=b"# notes",
        headers={
            "content-type": "text/plain;charset=utf-8",
            "content-disposition": 'attachment; filename="Notes-v1.md"',
        },
    )
    refused = client.get(ARTIFACT_PATH, params={"format": format})
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "unsupported_format"
    assert "notes" not in refused.text


def test_a_chat_answer_downloads_as_a_document_only(relay):
    client, _, seen, reply = relay
    reply["answer"] = lambda request: document("pdf")
    response = client.get(ANSWER_PATH, params={"format": "pdf"})
    assert response.status_code == 200 and response.headers["content-type"] == MIME["pdf"]
    assert seen[0].url.path == ANSWER_PATH.removeprefix("/api/control/pi")
    for query in ({"format": "csv"}, {"format": "json"}, {}):
        assert client.get(ANSWER_PATH, params=query).status_code == 422
    bad = "/api/control/pi/sessions/ses_x/messages/msg_" + "2" * 16 + "/download"
    assert client.get(bad, params={"format": "pdf"}).status_code == 422
    assert len(seen) == 1


def test_downloads_need_the_session_and_exact_query(relay):
    client, _, seen, _ = relay
    for query in (
        "",
        "?format=docx&format=pdf",
        "?format=docx&version=0",
        "?format=docx&version=101",
        "?format=docx&version=01",
        "?format=docx&version=-1",
        "?format=docx&version=+1",
        "?format=docx&version=1.0",
        "?format=docx&version=abc",
        "?format=docx&version=",
        "?format=docx&version=1&version=2",
        "?format=native",
        "?format=html",
    ):
        assert client.get(ARTIFACT_PATH + query).status_code == 422, query
    assert client.get("/api/control/pi/artifacts/artifact_x/download?format=pdf").status_code == 422
    assert not seen
    client.cookies.clear()
    assert client.get(ARTIFACT_PATH + "?format=docx").status_code == 401
    assert client.get(ANSWER_PATH + "?format=docx").status_code == 401
    assert not seen


def test_an_artifact_download_can_ask_for_an_earlier_version(relay):
    client, _, seen, _ = relay
    response = client.get(ARTIFACT_PATH + "?format=docx&version=3")
    assert response.status_code == 200, response.text
    [request] = seen
    assert request.url.params["format"] == "docx" and request.url.params["version"] == "3"


def test_the_answer_download_refuses_a_version(relay):
    client, _, seen, _ = relay
    assert client.get(ANSWER_PATH + "?format=docx&version=1").status_code == 422
    assert not seen


def test_pis_refusal_passes_through_as_its_own_json(relay):
    client, _, _, reply = relay
    refusal = {"detail": {"code": "export_too_long", "message": "This is too long to export."}}
    reply["answer"] = lambda request: httpx.Response(
        422, json=refusal, headers={"set-cookie": "upstream=secret"}
    )
    response = client.get(ARTIFACT_PATH + "?format=docx")
    assert response.status_code == 422 and response.json() == refusal
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: document(**{"content-type": "text/html"}),
        lambda request: document(**{"content-disposition": 'inline; filename="Plan-v1.docx"'}),
        lambda request: document(**{"content-disposition": 'attachment; filename="../x.docx"'}),
        lambda request: document(**{"content-disposition": 'attachment; filename="Plan-v1.exe"'}),
        lambda request: document(
            **{"content-disposition": "attachment; filename=\"a.docx\"; filename*=UTF-8''%2F.docx"}
        ),
        lambda request: document(**{"content-disposition": ""}),
        lambda request: streamed(document(), **{"content-encoding": "gzip"}),
        lambda request: httpx.Response(307, headers={"location": "https://elsewhere.test/"}),
        lambda request: httpx.Response(500, text="upstream-private-secret"),
    ],
    ids=[
        "html",
        "inline",
        "path",
        "extension",
        "readable-path",
        "no-name",
        "encoded",
        "redirect",
        "unreadable-refusal",
    ],
)
def test_a_file_outside_the_contract_never_reaches_the_browser(relay, answer):
    client, _, _, reply = relay
    reply["answer"] = answer
    response = client.get(ARTIFACT_PATH + "?format=docx")
    assert response.status_code == 502
    assert "document bytes" not in response.text and "secret" not in response.text


def test_a_file_over_16_mb_is_refused_before_it_streams(relay):
    client, _, _, reply = relay
    reply["answer"] = lambda request: document(**{"content-length": str(16 * 1024 * 1024 + 1)})
    response = client.get(ARTIFACT_PATH + "?format=docx")
    assert response.status_code == 413 and response.json()["detail"]["code"] == "too_large"


def test_an_unavailable_pi_is_503_and_never_retried(relay):
    client, _, seen, reply = relay

    def down(request):
        raise httpx.ConnectError("upstream-private-secret")

    reply["answer"] = down
    response = client.get(ARTIFACT_PATH + "?format=docx")
    assert response.status_code == 503 and "secret" not in response.text
    assert len(seen) == 1


def preview_reply(request):
    return httpx.Response(200, json={"digest": hashlib.sha256(request.content).hexdigest()})


def test_a_preview_streams_the_raw_export_with_the_session_alone(relay):
    client, headers, seen, reply = relay
    reply["answer"] = preview_reply
    path = "/api/control/pi/imports/chatgpt/preview"
    sent = {**headers, "Content-Type": "application/json"}
    response = client.post(path, content=EXPORT, headers=sent)
    assert response.status_code == 200, response.text
    assert response.json() == {"digest": DIGEST}
    [request] = seen
    assert str(request.url) == "http://pi.test/imports/chatgpt/preview"
    assert request.content == EXPORT
    assert request.headers["x-pi-owner-key"] == OWNER
    assert request.headers["content-type"] == "application/json"
    assert "cookie" not in request.headers and "x-conker-verification" not in request.headers
    # Signed in, same origin and CSRF: the usual session rules for a write.
    assert (
        client.post(path, content=EXPORT, headers={"Content-Type": "application/json"}).status_code
        == 403
    )
    assert client.post(path + "?x=1", content=EXPORT, headers=sent).status_code == 422
    plain = {**headers, "Content-Type": "text/plain"}
    assert client.post(path, content=EXPORT, headers=plain).status_code == 415
    assert (
        client.post(
            "/api/control/pi/imports/gemini/preview", content=EXPORT, headers=sent
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/control/pi/imports/claude/" + "A" * 64, content=EXPORT, headers=sent
        ).status_code
        == 422
    )
    assert len(seen) == 1
    client.cookies.clear()
    assert client.post(path, content=EXPORT, headers=sent).status_code == 401
    assert len(seen) == 1


def test_pis_import_refusal_passes_through(relay):
    client, headers, _, reply = relay
    refusal = {"detail": {"code": "not_json", "message": "This file is not valid JSON."}}
    reply["answer"] = lambda request: httpx.Response(422, json=refusal)
    response = client.post(
        "/api/control/pi/imports/claude/preview",
        content=b"[",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 422 and response.json() == refusal


def test_the_128_mb_limit_holds_whether_declared_or_streamed(relay, monkeypatch):
    client, headers, seen, reply = relay
    reply["answer"] = preview_reply
    path = "/api/control/pi/imports/chatgpt/preview"
    sent = {**headers, "Content-Type": "application/json"}
    declared = {**sent, "Content-Length": str(128 * 1024 * 1024 + 1)}
    response = client.post(path, content=b"[]", headers=declared)
    assert response.status_code == 413 and response.json()["detail"]["code"] == "too_large"
    assert not seen
    monkeypatch.setattr(file_routes, "MAX_IMPORT_BYTES", 8)
    streamed = client.post(path, content=iter([b"[1,2,3,", b"4,5,6]"]), headers=sent)
    assert streamed.status_code == 413 and streamed.json()["detail"]["code"] == "too_large"
    assert not seen
    # Other owner writes keep their own small limit.
    big = client.post(
        "/api/control/pi/models/configuration",
        json={"text": "x" * 70000},
        headers=headers,
    )
    assert big.status_code == 413


def test_an_import_needs_the_password_for_exactly_the_previewed_file(relay):
    client, headers, seen, reply = relay
    reply["answer"] = lambda request: httpx.Response(200, json={"imported": True})
    path = f"/api/control/pi/imports/claude/{DIGEST}"
    sent = {**headers, "Content-Type": "application/json"}
    response = client.post(path, content=EXPORT, headers=sent)
    assert response.status_code == 428 and not seen
    # A proof for another file's import does not admit this one.
    other = proof(client, sent, "/api/control/pi/imports/claude/" + "b" * 64, {})
    assert client.post(path, content=EXPORT, headers=other).status_code == 428
    checked = proof(client, sent, path, {})
    response = client.post(path, content=EXPORT, headers=checked)
    assert response.status_code == 200 and response.json() == {"imported": True}
    [request] = seen
    assert request.content == EXPORT and request.url.path == f"/imports/claude/{DIGEST}"
    assert "x-conker-verification" not in request.headers
    assert client.post(path, content=EXPORT, headers=checked).status_code == 428
    # A different body under the previewed digest is refused before Pi has all of it; the proof
    # was spent on admission, so trying again asks for the password again.
    checked = proof(client, sent, path, {})
    changed = client.post(path, content=EXPORT + b" ", headers=checked)
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "file_changed"
    assert len(seen) == 1
    assert client.post(path, content=EXPORT, headers=checked).status_code == 428


def test_the_import_archive_is_read_with_the_session(relay):
    client, _, seen, reply = relay
    reply["answer"] = lambda request: httpx.Response(200, json={"results": []})
    chat = "/api/control/pi/imports/chats/imp_" + "0" * 32
    for path in (
        "/api/control/pi/imports",
        "/api/control/pi/imports/chats?source=chatgpt&limit=5&offset=10",
        chat + "?after=3&limit=20",
    ):
        response = client.get(path)
        assert response.status_code == 200 and response.json() == {"results": []}
        assert seen[-1].headers["x-pi-owner-key"] == OWNER
        assert str(seen[-1].url) == "http://pi.test" + path.removeprefix("/api/control/pi")
    assert client.get("/api/control/pi/imports/chats/imp_x").status_code == 403
    client.cookies.clear()
    assert client.get("/api/control/pi/imports").status_code == 401
    assert len(seen) == 3
