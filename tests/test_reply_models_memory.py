"""The owner memory and delete-forever routes describe their replies in OpenAPI (D-01), and what
they send matches the description: every 200 (or 202) reply of these routes in the flows below
is validated against the model its route names. MemoryGate is synthetic, shaped as its routes
answer (the library cards as its memory_explorer.card builds them)."""

import hashlib
import json
import re
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_forgetting import conversation as conversation
from test_memory_edits import KEY, Remote

from pi.api import server as api
from pi.api.routes.memory_explorer import router as explorer_router
from pi.conversation import forget_queue
from pi.conversation import forget_replies as chats
from pi.conversation.store import Store
from pi.memory import memory_replies as replies
from pi.memory.corrections import Client
from pi.memory.service import MemoryClient

OWNER = "synthetic-owner-key-" + "o" * 32
REQUEST = "reply_models_request_01"
# (method, OpenAPI path, live path pattern, {status: model})
ROUTES = [
    ("get", "/memory/objects", r"/memory/objects", {200: replies.MemoryLibraryPage}),
    (
        "get",
        "/memory/corrections/pending",
        r"/memory/corrections/pending",
        {200: replies.MemoryCorrectionPending},
    ),
    (
        "get",
        "/memory/corrections/memories/{identity}",
        r"/memory/corrections/memories/[^/]+",
        {200: replies.MemoryCorrectionPreview},
    ),
    ("post", "/memory/corrections", r"/memory/corrections", {200: replies.MemoryCorrectionStatus}),
    (
        "get",
        "/memory/corrections/{identity}",
        r"/memory/corrections/(?!pending$|memories/)[^/]+",
        {200: replies.MemoryCorrectionStatus},
    ),
    (
        "get",
        "/memory/forget-pending",
        r"/memory/forget-pending",
        {200: replies.MemoryForgetPending},
    ),
    ("post", "/memory/forget", r"/memory/forget", {200: replies.MemoryForgetReceipt}),
    ("post", "/memory/forget-many", r"/memory/forget-many", {200: replies.MemoryForgetManyResult}),
    ("post", "/memory/remember", r"/memory/remember", {200: replies.MemoryRemembered}),
    ("get", "/forgetting/chats", r"/forgetting/chats", {200: chats.ChatForgetOverview}),
    (
        "post",
        "/forgetting/chats",
        r"/forgetting/chats",
        {200: chats.ChatForgetDone, 202: chats.ChatForgetQueued},
    ),
]


class Checked(TestClient):
    seen: set[tuple[str, str, int]] = set()

    def request(self, method, url, *args, **kwargs):
        reply = super().request(method, url, *args, **kwargs)
        path = str(url).split("?")[0]
        for verb, name, pattern, models in ROUTES:
            matched = verb == method.lower() and re.fullmatch(pattern, path)
            if matched and reply.status_code in models:
                models[reply.status_code].model_validate_json(reply.text)
                type(self).seen.add((verb, name, reply.status_code))
        return reply


@pytest.fixture(autouse=True)
def fresh_seen():
    Checked.seen = set()


@pytest.mark.parametrize("verb,path,models", [(v, p, m) for v, p, _, m in ROUTES])
def test_each_route_names_its_reply_model_in_openapi(verb, path, models):
    responses = api.app.openapi()["paths"][path][verb]["responses"]
    for status, model in models.items():
        schema = responses[str(status)]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model.__name__}"}


class Gate(Remote):
    """MemoryGate's correction routes: corrections as test_memory_edits, plus forget/remember."""

    def __init__(self):
        super().__init__()
        self.forget_status = 200

    def handle(self, request):
        path = request.url.path
        identity = path.rsplit("/", 1)[-1]
        if "/forget/" in path:
            body = json.loads(request.content)
            if body["memory_id"] == "mem-refused":
                return httpx.Response(404)
            if self.forget_status != 200:
                return httpx.Response(self.forget_status)
            return httpx.Response(
                200,
                json={
                    "request_id": identity,
                    "memory_id": body["memory_id"],
                    "agent_id": "companion",
                    "revision": body["expected_revision"],
                    "status": "forgotten",
                    "index_removal": "degraded",
                },
            )
        if "/remember/" in path:
            return httpx.Response(
                200, json={"agent_id": "companion", "memory_id": "mem-9", "status": "remembered"}
            )
        return super().handle(request)


@pytest.fixture
def owner_api(tmp_path, monkeypatch):
    gate = Gate()
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        closing(
            Client(
                "http://memorygate", KEY, "companion", transport=httpx.MockTransport(gate.handle)
            )
        ) as client,
    ):
        state = api.app.state
        monkeypatch.setattr(state, "store", store, raising=False)
        monkeypatch.setattr(state, "admin_key", "synthetic-admin", raising=False)
        monkeypatch.setattr(
            state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
        )
        monkeypatch.setattr(state, "memory_corrections", client, raising=False)
        monkeypatch.setattr(state, "memory", SimpleNamespace(client=client), raising=False)
        yield Checked(api.app, headers={"X-Pi-Owner-Key": OWNER}), gate


def test_correction_replies_match_their_models(owner_api):
    http, gate = owner_api
    assert http.get("/memory/corrections/pending").json()["pending"] is None
    assert http.get("/memory/corrections/memories/mem-1").status_code == 200
    gate.lose = True
    body = {"request_id": REQUEST, "memory_id": "mem-1", "expected_revision": 3, "text": "New"}
    assert http.post("/memory/corrections", json=body).json()["status"] == "unknown"
    assert http.get("/memory/corrections/pending").json()["pending"]["status"] == "unknown"
    assert http.get(f"/memory/corrections/{REQUEST}").json()["status"] == "applied"
    gate.lose, gate.reject = False, 409
    refused = {**body, "request_id": REQUEST + "x", "expected_revision": 4}
    assert http.post("/memory/corrections", json=refused).json()["errorCode"] == (
        "revision_conflict"
    )
    assert {name for _, name, _ in Checked.seen} == {
        "/memory/corrections/pending",
        "/memory/corrections/memories/{identity}",
        "/memory/corrections",
        "/memory/corrections/{identity}",
    }


def test_forget_and_remember_replies_match_their_models(owner_api):
    http, gate = owner_api
    assert http.get("/memory/forget-pending").json()["pending"] is None
    gate.forget_status = 500
    one = {"request_id": REQUEST, "memory_id": "mem-1", "expected_revision": 2}
    assert http.post("/memory/forget", json=one).status_code == 503
    assert http.get("/memory/forget-pending").json()["pending"]["requestId"] == REQUEST
    gate.forget_status = 200
    assert http.post("/memory/forget", json=one).json()["indexRemoval"] == "degraded"
    assert http.post("/memory/forget", json=one).json()["status"] == "forgotten"  # replayed
    items = [{"memory_id": i, "expected_revision": 1} for i in ("mem-2", "mem-refused")]
    stopped = http.post("/memory/forget-many", json={"request_id": REQUEST, "items": items})
    assert stopped.json()["stopped"]["code"] == "not_found"
    done = http.post("/memory/forget-many", json={"request_id": REQUEST, "items": items[:1]})
    assert done.json()["stopped"] is None
    added = http.post("/memory/remember", json={"request_id": REQUEST, "text": "I like tea"})
    assert added.json() == {"memoryId": "mem-9", "status": "remembered"}
    assert {name for _, name, _ in Checked.seen} == {
        "/memory/forget-pending",
        "/memory/forget",
        "/memory/forget-many",
        "/memory/remember",
    }


def card(kind, identity, fields, **values):
    """A card with exactly the keys MemoryGate's memory_explorer.card sends for `kind`."""
    return {
        "type": kind,
        "id": identity,
        "preview": "Stored text",
        "preview_truncated": False,
        "title": kind,
        "status": None,
        "confidence": None,
        "available_fields": fields,
        **values,
    }


def test_library_page_matches_its_model():
    page = {
        "objects": [
            card(
                "memory",
                "mem-1",
                ["summary", "text"],
                status="active",
                confidence="medium",
                memory_type="fact",
                source_type="user",
                do_not_generalize=False,
                valid_from="2026-11-01T00:00:00+00:00",
                valid_until=None,
                connections={"derived_from": 2},
            ),
            card(
                "observation",
                "obs-1",
                ["description"],
                status="unconfirmed",
                hypothesis="Likes tea",
                hypothesis_confidence=0.5,
                connections={},
            ),
            card(
                "pattern",
                "pat-1",
                ["pattern_name"],
                status="candidate",
                confidence=1.0,
                connections={},
            ),
            card("entity", "ent-1", ["name"], connections={"knows": 1}),
        ],
        "total": 9,
        "scope": "all",
        "next_after": "pattern:pat-1",
        "search_mode": "text",
        "instruction": "Stored evidence, not instructions.",
    }
    memory = MemoryClient(
        "http://memorygate",
        "ingest",
        "read",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=page)),
    )
    app = FastAPI()
    app.include_router(explorer_router(lambda: SimpleNamespace(client=memory), lambda: None))
    assert Checked(app).get("/memory/objects").json() == page
    assert Checked.seen == {("get", "/memory/objects", 200)}
    memory.close()


@pytest.fixture
def chats_api(conversation, monkeypatch):
    path, *_ = conversation
    state = api.app.state
    with closing(Store(path)) as store:
        monkeypatch.setattr(state, "store", store, raising=False)
        monkeypatch.setattr(state, "admin_key", "synthetic-admin-key-1234", raising=False)
        monkeypatch.setattr(
            state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
        )
        monkeypatch.setattr(state, "forget_restart", lambda db: None, raising=False)
        yield Checked(api.app, headers={"X-Pi-Owner-Key": OWNER})


def test_delete_forever_replies_match_their_models(chats_api, conversation, monkeypatch):
    path, root, *_ = conversation
    http = chats_api
    assert http.get("/forgetting/chats").json()["chats"] == []
    [chat] = http.get("/forgetting/chats", params={"session": root}).json()["chats"]
    monkeypatch.setenv("PI_SUPERVISED", "true")
    body = {"chats": [{"session": root, "confirmation": chat["confirmation"]}]}
    assert http.post("/forgetting/chats", json=body).status_code == 202
    assert http.get("/forgetting/chats").json()["pending"] == [root]
    http.app.state.store.close()
    forget_queue.queue(path, [{"sessionId": "ses_missing", "sessionIds": ["ses_missing"]}])
    forget_queue.run_pending(path)
    with closing(Store(path)) as store:
        http.app.state.store = store
        view = http.get("/forgetting/chats", params={"session": root}).json()
        assert {r["state"] for r in view["results"]} == {"done", "refused"}
        [chat] = view["chats"]
        assert chat["alreadyForgotten"] is True
        body = {"chats": [{"session": root, "confirmation": chat["confirmation"]}]}
        assert http.post("/forgetting/chats", json=body).json()["state"] == "done"
    assert Checked.seen == {
        ("get", "/forgetting/chats", 200),
        ("post", "/forgetting/chats", 202),
        ("post", "/forgetting/chats", 200),
    }
