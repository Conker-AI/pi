"""Tool request routes: Pi's owner routes, the gateway's proxies for them, and publishing for Conker."""

import hashlib
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_gateway_mcp import boundary, proof  # noqa: F401 - fixture

from gateway.mutation import apply_resource
from pi.actions.toolgate import ToolGateClient, ToolGateUnavailable
from pi.agents import change_requests, tool_drafts
from pi.api import server as api
from pi.api.browser_contract import owner_allowed, runtime_allowed, session_only_write
from pi.conversation.store import Store
from tests.test_gateway_mutation import config

OWNER = "tool-drafts-owner-key-" + "o" * 32
REQUEST = "/change-requests/creq_" + "a" * 32
TOOL_ID = "workflow:topic-reader:1:" + "c" * 64
PUBLISH = "/api/owner/editor-drafts/topic-reader/publish"
PUBLISHED = {"expected_revision": 2, "expected_publication_version": 0, "authorization": "auto"}


class Drafts:
    def __init__(self):
        self.kicks = 0

    def kick(self):
        self.kicks += 1


class Loop:
    def __init__(self):
        self.drafts = Drafts()


@pytest.fixture()
def pi(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    loop = Loop()
    for name, value in (
        ("store", store),
        ("loop", loop),
        ("admin_key", "synthetic-admin"),
        ("owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest()),
        ("toolgate", None),
    ):
        monkeypatch.setattr(api.app.state, name, value, raising=False)
    return TestClient(api.app), store, loop


def test_the_three_routes_are_owner_routes_without_a_password():
    for path in ("/tool-drafts", REQUEST + "/retry", REQUEST + "/published"):
        assert owner_allowed("POST", path) and session_only_write("POST", path)
        assert not runtime_allowed("POST", path)
    for path in ("/tool-drafts/x", REQUEST + "/publish", "/change-requests/creq_x/retry"):
        assert not owner_allowed("POST", path) and not session_only_write("POST", path)
    assert not session_only_write("POST", REQUEST + "/approve")


def test_a_tool_draft_without_a_chat_needs_the_owner_and_starts_drafting(pi):
    client, store, loop = pi
    body = {"description": "Search the web for a topic", "request_id": "tool-draft-route-01"}
    assert client.post("/tool-drafts", json=body).status_code == 401
    assert (
        client.post("/tool-drafts", json=body, headers={"X-Pi-Owner-Key": "x"}).status_code == 401
    )
    owner = {"X-Pi-Owner-Key": OWNER}
    first = client.post("/tool-drafts", json=body, headers=owner)
    assert first.status_code == 200, first.text
    request = first.json()
    assert request["kind"] == "tool_draft" and request["state"] == "open"
    assert request["payload"] == {"description": body["description"], "status": "drafting"}
    assert loop.drafts.kicks == 1
    again = client.post("/tool-drafts", json=body, headers=owner)
    assert again.json()["id"] == request["id"] and len(change_requests.listing(store)) == 1
    reused = client.post("/tool-drafts", json={**body, "description": "Other"}, headers=owner)
    assert reused.status_code == 409 and reused.json()["detail"]["code"] == "request_reused"
    for bad in (
        {"description": "", "request_id": "tool-draft-route-02"},
        {"description": "x" * 4001, "request_id": "tool-draft-route-02"},
        {"description": "x", "request_id": "short"},
        {"description": "x"},
        {**body, "sessionId": "ses_1"},
    ):
        assert client.post("/tool-drafts", json=bad, headers=owner).status_code == 422
    # The recovery key still works for the command line.
    admin = client.post(
        "/tool-drafts",
        json={**body, "request_id": "tool-draft-route-03"},
        headers={"X-Pi-Key": "synthetic-admin"},
    )
    assert admin.status_code == 200


def test_retry_and_published_need_the_owner_and_a_tool_request(pi):
    client, store, loop = pi
    owner = {"X-Pi-Owner-Key": OWNER}
    session = store.create_session()
    filed = tool_drafts.file(store, "Search the web", session_id=session)
    path = f"/change-requests/{filed['id']}"
    assert client.post(path + "/retry", json={}).status_code == 401
    busy = client.post(path + "/retry", json={}, headers=owner)
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "drafting"
    tool_drafts.recover_interrupted(store)
    retried = client.post(path + "/retry", json={"description": "Search Bing"}, headers=owner)
    assert retried.status_code == 200
    assert retried.json()["payload"] == {"description": "Search Bing", "status": "drafting"}
    assert loop.drafts.kicks == 1
    tool_drafts.recover_interrupted(store)
    assert client.post(path + "/retry", headers=owner).status_code == 200
    assert client.post(path + "/retry", json={"extra": 1}, headers=owner).status_code == 422
    body = {"toolId": TOOL_ID, "name": "Topic reader"}
    assert client.post(path + "/published", json=body).status_code == 401
    for bad in ({"toolId": "x y", "name": "Topic"}, {"toolId": TOOL_ID}, {**body, "extra": 1}):
        assert client.post(path + "/published", json=bad, headers=owner).status_code == 422
    published = client.post(path + "/published", json=body, headers=owner)
    assert published.status_code == 200, published.text
    assert published.json()["result"]["published"]["toolId"] == TOOL_ID
    assert published.json()["state"] == "approved"
    [note] = store.messages(session)
    assert note["role"] == "assistant"
    assert note["content"] == "“Topic reader” is live. Want me to run it now?"
    assert client.post(path + "/published", json=body, headers=owner).status_code == 200
    assert len(store.messages(session)) == 1  # said once
    memory = change_requests.file(store, "memory", "Remember: x", "x", {"text": "x"})
    wrong = client.post(f"/change-requests/{memory['id']}/retry", json={}, headers=owner)
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "not_a_tool_draft"
    assert client.post(REQUEST + "/retry", json={}, headers=owner).status_code == 404


def test_the_inbox_answers_the_drafters_question_through_retry(pi):
    client, store, loop = pi
    owner = {"X-Pi-Owner-Key": OWNER}
    filed = tool_drafts.file(store, "Search the web", session_id=store.create_session())
    path = f"/change-requests/{filed['id']}/retry"
    [request] = tool_drafts.pending(store)
    tool_drafts.finish(store, request, tool_drafts.needs_detail("Search the web", "Which engine?"))
    both = client.post(path, json={"description": "x", "answer": "y"}, headers=owner)
    assert both.status_code == 422
    answered = client.post(path, json={"answer": "Bing"}, headers=owner)
    assert answered.status_code == 200, answered.text
    assert answered.json()["payload"] == {
        "description": "Search the web",
        "status": "drafting",
        "conversation": [
            {"role": "builder", "content": "Which engine?"},
            {"role": "owner", "content": "Bing"},
        ],
    }
    assert loop.drafts.kicks == 1
    tool_drafts.recover_interrupted(store)
    again = client.post(path, json={"answer": "Google"}, headers=owner)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_asking"


def test_a_request_made_without_a_chat_is_published_without_a_note(pi):
    client, store, _ = pi
    filed = tool_drafts.submit(
        store, tool_drafts.NewDraft(description="Search", request_id="tool-draft-route-04")
    )
    body = {"toolId": TOOL_ID, "name": "Topic reader"}
    response = client.post(
        f"/change-requests/{filed['id']}/published", json=body, headers={"X-Pi-Owner-Key": OWNER}
    )
    assert response.status_code == 200 and response.json()["sessionId"] is None


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/tool-drafts", {"description": "Search the web", "request_id": "tool-draft-gw-0001"}),
        (REQUEST + "/retry", {"description": "Search Bing"}),
        (REQUEST + "/retry", {"answer": "Bing"}),
        (REQUEST + "/retry", {}),
        (REQUEST + "/published", {"toolId": TOOL_ID, "name": "Topic reader"}),
    ],
)
def test_the_gateway_proxies_them_for_the_signed_in_owner_without_a_password(boundary, path, body):
    import dataclasses

    app, client, headers, seen, _ = boundary
    app.state.config = dataclasses.replace(app.state.config, pi_owner_key="p" * 32)
    gateway_path = "/api/control/pi" + path
    for invalid in (
        {},
        {**headers, "Origin": "https://elsewhere.test"},
        {**headers, "X-CSRF-Token": "x"},
    ):
        assert client.post(gateway_path, json=body, headers=invalid).status_code == 403
    assert client.post(gateway_path + "?again=1", json=body, headers=headers).status_code == 422
    assert not seen
    response = client.post(gateway_path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    [request] = seen
    assert request.url == "http://pi.test" + path
    assert request.headers["x-pi-owner-key"] == "p" * 32
    assert "x-conker-verification" not in request.headers and "cookie" not in request.headers
    assert json.loads(request.content) == body


def test_publishing_for_conker_keeps_the_password_and_names_conkers_key(boundary):
    _, client, headers, seen, _ = boundary
    body = {**PUBLISHED, "conkerMayUse": True}
    assert client.post(PUBLISH, json=body, headers=headers).status_code == 428
    checked = proof(client, headers, PUBLISH, body)
    # The proof covers the switch: the same proof cannot publish without it, or the other way.
    assert client.post(PUBLISH, json=PUBLISHED, headers=checked).status_code == 428
    assert client.post(PUBLISH, json=body, headers=checked).status_code == 200
    [request] = seen
    assert request.url == "http://toolgate.test/v2/owner/editor-drafts/topic-reader/publish"
    assert json.loads(request.content) == body
    assert request.headers["x-toolgate-owner-key"] and request.headers["x-toolgate-execution-key"]
    assert client.post(PUBLISH, json=body, headers=checked).status_code == 428  # used once


@pytest.mark.parametrize("switch", [{}, {"conkerMayUse": False}])
def test_publishing_without_the_switch_sends_no_chat_key(boundary, switch):
    _, client, headers, seen, _ = boundary
    body = {**PUBLISHED, **switch}
    response = client.post(PUBLISH, json=body, headers=proof(client, headers, PUBLISH, body))
    assert response.status_code == 200
    [request] = seen
    assert json.loads(request.content) == body
    assert "x-toolgate-execution-key" not in request.headers


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_the_switch_must_be_true_or_false(boundary, value):
    _, client, headers, seen, _ = boundary
    response = client.post(PUBLISH, json={**PUBLISHED, "conkerMayUse": value}, headers=headers)
    assert response.status_code == 422 and not seen


def test_publishing_for_conker_needs_the_chat_key_on_the_host(boundary):
    import dataclasses

    app, client, headers, seen, _ = boundary
    app.state.config = dataclasses.replace(app.state.config, toolgate_execution_key="")
    body = {**PUBLISHED, "conkerMayUse": True}
    response = client.post(PUBLISH, json=body, headers=proof(client, headers, PUBLISH, body))
    assert response.status_code == 503 and not seen


@pytest.mark.parametrize(
    ("payload", "path", "body"),
    [
        ({"operation": "retry", "id": "creq_" + "d" * 32}, "/retry", {}),
        (
            {"operation": "retry", "id": "creq_" + "d" * 32, "description": "Search Bing"},
            "/retry",
            {"description": "Search Bing"},
        ),
        (
            {"operation": "published", "id": "creq_" + "d" * 32, "toolId": TOOL_ID, "name": "T"},
            "/published",
            {"toolId": TOOL_ID, "name": "T"},
        ),
    ],
)
def test_the_cli_retries_and_reports_publications_through_the_owner_routes(payload, path, body):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"id": payload["id"]})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        apply_resource(config(), "requests", io.BytesIO(json.dumps(payload).encode()), client)
    [request] = seen
    assert request.url.path == "/change-requests/" + payload["id"] + path
    assert owner_allowed("POST", request.url.path) and json.loads(request.content) == body


@pytest.mark.parametrize("conker", [True, False])
def test_the_cli_publishes_for_conker_with_the_chat_key(conker):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"toolId": TOOL_ID})

    payload = {"operation": "publish", "id": "topic-reader", **PUBLISHED, "conkerMayUse": conker}
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        apply_resource(config(), "tools", io.BytesIO(json.dumps(payload).encode()), client)
    [request] = seen
    assert request.url.path == "/v2/owner/editor-drafts/topic-reader/publish"
    assert json.loads(request.content) == {
        **PUBLISHED,
        **({"conkerMayUse": True} if conker else {}),
    }
    assert ("x-toolgate-execution-key" in request.headers) is conker


def checked(monkeypatch, answer):
    sent = []

    def post(url, **options):
        sent.append((url, options))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "post", post)
    client = ToolGateClient("http://toolgate.test", "tgx_" + "k" * 40)
    return client, sent


def test_toolgates_document_check_is_read_with_conkers_key(monkeypatch):
    issues = [{"code": "missing_tool", "message": "No such tool", "nodeId": "fetch"}]
    client, sent = checked(
        monkeypatch, httpx.Response(200, json={"valid": False, "issues": issues})
    )
    assert client.check_document({"id": "x"}) == {"valid": False, "issues": issues}
    [(url, options)] = sent
    assert url == "http://toolgate.test/v2/editor/check"
    assert options["json"] == {"document": {"id": "x"}}
    assert options["headers"]["X-ToolGate-Execution-Key"] == "tgx_" + "k" * 40


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("down"),
        httpx.Response(404, json={"detail": "Not Found"}),
        # A wrong-shaped document is a 200 with issues; a 422 means the request itself was off.
        httpx.Response(422, json={"detail": "Request validation failed."}),
        httpx.Response(401, json={"detail": "bad key"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, json={"valid": "yes", "issues": []}),
        httpx.Response(200, json=[]),
    ],
)
def test_a_toolgate_that_cannot_check_is_unavailable(monkeypatch, answer):
    client, _ = checked(monkeypatch, answer)
    with pytest.raises(ToolGateUnavailable):
        client.check_document({"id": "x"})
