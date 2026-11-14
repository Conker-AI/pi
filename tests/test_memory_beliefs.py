"""GET /memory/beliefs: what Conker believes about the owner, proxied from MemoryGate (R-M5)."""

from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from pi.api.browser_contract import owner_allowed
from pi.api.routes import memory as memory_api
from pi.conversation.store import Store
from pi.core import errors
from pi.memory.beliefs import BeliefPage
from pi.memory.service import MemoryClient


def _item(identity, origin="stated", state="current"):
    return {
        "id": identity,
        "revision": 2,
        "text": "I prefer green tea in the afternoon",
        "memory_type": "context",
        "origin": origin,
        "source_type": "owner_statement" if origin == "stated" else "analysis",
        "confidence": "medium",
        "state": state,
        "valid_from": None,
        "valid_until": None,
        "review_by": None,
        "created_at": "2026-11-01T09:00:00+00:00",
        "updated_at": "2026-11-01T09:00:00+00:00",
        "last_recalled_at": None,
        "evidence": {"supporting": 1, "withdrawn": 0},
        "lineage": [{"object_type": "analysis", "object_id": "a1", "withdrawn": False}],
        "conflicts": [{"conflict_id": "c1", "memory_id": "m7"}],
    }


def _page(items, total=None, agent="companion"):
    stated = sum(item["origin"] == "stated" for item in items)
    review = sum(item["state"] == "needs_review" for item in items)
    return {
        "agent_id": agent,
        "total": len(items) if total is None else total,
        "counts": {"stated": stated, "concluded": len(items) - stated, "needs_review": review},
        "items": items,
        "next_after": "eyJjdXJzb3IiOiAxfQ",
    }


def _forget(store, memory_id, state):
    with store._connect() as db:
        db.execute(
            "INSERT INTO memory_forget_operations"
            "(request_id,memory_id,agent_id,destination,expected_revision,state)"
            " VALUES (?,?,?,?,?,?)",
            ("r" + memory_id, memory_id, "companion", "d", 1, state),
        )


def _app(upstream, *, configured=True, store=None, owner=True):
    seen = []

    def handler(request):
        seen.append(request)
        return upstream(request)

    client = MemoryClient(
        "http://memorygate",
        "ingest-private",
        "read-private",
        "companion",
        transport=httpx.MockTransport(handler),
    )
    memory = SimpleNamespace(client=client if configured else None, store=store)

    def require_owner(request: Request):
        if not owner:
            raise HTTPException(401, "Owner session required.")

    app = FastAPI()
    errors.install(app)
    app.include_router(
        memory_api.router(lambda: SimpleNamespace(memory=memory), lambda: None, require_owner)
    )
    return TestClient(app), seen


def test_reply_is_camel_case_and_validates_against_the_named_model():
    client, seen = _app(lambda request: httpx.Response(200, json=_page([_item("m1")])))
    response = client.get("/memory/beliefs")
    assert response.status_code == 200
    body = response.json()
    BeliefPage.model_validate(body)
    assert body["agentId"] == "companion"
    assert body["counts"] == {"stated": 1, "concluded": 0, "needsReview": 0}
    assert body["nextAfter"] == "eyJjdXJzb3IiOiAxfQ"
    item = body["items"][0]
    assert item["memoryType"] == "context" and item["lastRecalledAt"] is None
    assert item["lineage"] == [{"objectType": "analysis", "objectId": "a1", "withdrawn": False}]
    assert item["conflicts"] == [{"conflictId": "c1", "memoryId": "m7"}]
    request = seen[0]
    assert request.url.path == "/runtime/memory-beliefs"
    assert request.headers["x-memorygate-key"] == "read-private"
    assert request.headers["x-agent-id"] == "companion"
    assert "x-memorygate-conversation-key" not in request.headers


def test_openapi_names_the_reply_model():
    client, _ = _app(lambda request: httpx.Response(200, json=_page([])))
    operation = client.get("/openapi.json").json()["paths"]["/memory/beliefs"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/BeliefPage"}


def test_filters_and_cursor_reach_memorygate_unchanged():
    client, seen = _app(lambda request: httpx.Response(200, json=_page([])))
    response = client.get(
        "/memory/beliefs?origin=concluded&state=needs_review&memory_type=fact&limit=7"
        "&after=eyJ4IjogMX0"
    )
    assert response.status_code == 200
    assert dict(seen[0].url.params) == {
        "origin": "concluded",
        "state": "needs_review",
        "memory_type": "fact",
        "limit": "7",
        "after": "eyJ4IjogMX0",
    }
    client.get("/memory/beliefs")
    assert dict(seen[1].url.params) == {"origin": "all", "state": "all", "limit": "50"}


def test_a_refused_cursor_stays_a_422():
    client, _ = _app(lambda request: httpx.Response(422, json={"detail": "Invalid cursor"}))
    response = client.get("/memory/beliefs?after=not-handed-out")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"


def test_bad_filters_are_refused_before_memorygate():
    client, seen = _app(lambda request: httpx.Response(200, json=_page([])))
    for query in ("origin=guessed", "state=gone", "memory_type=secret", "limit=0", "limit=101"):
        assert client.get("/memory/beliefs?" + query).status_code == 422
    assert seen == []


def test_owner_session_is_required():
    client, seen = _app(lambda request: httpx.Response(200, json=_page([])), owner=False)
    assert client.get("/memory/beliefs").status_code == 401
    assert seen == []
    assert owner_allowed("GET", "/memory/beliefs")
    assert not owner_allowed("POST", "/memory/beliefs")


def test_a_memory_being_forgotten_is_left_out_of_items_and_counts(tmp_path):
    store = Store(tmp_path / "pi.db")
    _forget(store, "m2", "pending")
    _forget(store, "m3", "forgotten")
    _forget(store, "m4", "rejected")
    items = [
        _item("m1"),
        _item("m2", origin="concluded", state="needs_review"),
        _item("m3"),
        _item("m4", origin="concluded"),
    ]
    client, _ = _app(lambda request: httpx.Response(200, json=_page(items, total=40)), store=store)
    body = client.get("/memory/beliefs").json()
    assert [item["id"] for item in body["items"]] == ["m1", "m4"]
    assert body["total"] == 38
    assert body["counts"] == {"stated": 1, "concluded": 1, "needsReview": 0}


def test_unconfigured_or_silent_memory_is_503_never_an_empty_list():
    client, _ = _app(lambda request: httpx.Response(200, json=_page([])), configured=False)
    response = client.get("/memory/beliefs")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "memory_unavailable"

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    for upstream in (
        down,
        lambda request: httpx.Response(500, json={"detail": "private upstream body"}),
        lambda request: httpx.Response(200, json=_page([], agent="someone-else")),
        lambda request: httpx.Response(200, json={"agent_id": "companion", "items": "nope"}),
        lambda request: httpx.Response(200, content=b"not json"),
    ):
        client, _ = _app(upstream)
        response = client.get("/memory/beliefs")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "memory_unavailable"
        assert "private upstream" not in response.text
