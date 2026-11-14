"""GET /memory/upkeep: memory upkeep candidates, proxied from MemoryGate (R-M4)."""

from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from pi.api.browser_contract import owner_allowed
from pi.api.routes import memory as memory_api
from pi.conversation.store import Store
from pi.core import errors
from pi.memory import beliefs as memory_beliefs
from pi.memory.service import MemoryClient
from pi.memory.upkeep import UpkeepPage


def _memory(text="I take the early train", revision=1, state="current"):
    return {
        "text": text,
        "revision": revision,
        "memory_type": "fact",
        "source_type": "user",
        "state": state,
    }


def _kind(*items, total=None):
    return {"items": list(items), "total": len(items) if total is None else total}


def _reply(agent="companion", memories=True, **kinds):
    body = {
        "agent_id": agent,
        "checked_at": "2026-11-14T08:00:00+00:00",
        "memories_checked": 12,
        "truncated": False,
        "exact_duplicates": kinds.get("exact", _kind()),
        "near_duplicates": kinds.get("near", _kind()),
        "contradictions": kinds.get("contradictions", _kind()),
        "expired": kinds.get("expired", _kind()),
        "not_recalled": kinds.get("unused", _kind()),
    }
    if memories:
        named = {
            i
            for kind in body.values()
            if isinstance(kind, dict) and "items" in kind
            for item in kind["items"]
            for i in item["ids"]
        }
        body["memories"] = {i: _memory(text=f"words of {i}") for i in sorted(named)}
    return body


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


def test_reply_is_camel_case_and_keeps_memory_ids_as_they_are():
    reply = _reply(
        exact=_kind({"ids": ["m_one", "m_two"], "reason": "same text"}),
        contradictions=_kind({"ids": ["m_3", "m_4"], "conflict_id": "c_1", "reason": "differ"}),
    )
    client, seen = _app(lambda request: httpx.Response(200, json=reply))
    response = client.get("/memory/upkeep")
    assert response.status_code == 200
    body = response.json()
    UpkeepPage.model_validate(body)
    assert body["agentId"] == "companion" and body["memoriesChecked"] == 12
    assert body["exactDuplicates"]["items"] == [
        {"ids": ["m_one", "m_two"], "reason": "same text", "conflictId": None}
    ]
    assert body["contradictions"]["items"][0]["conflictId"] == "c_1"
    # Ids keep their underscores; only the fields inside are renamed.
    assert set(body["memories"]) == {"m_one", "m_two", "m_3", "m_4"}
    assert body["memories"]["m_one"] == {
        "text": "words of m_one",
        "revision": 1,
        "memoryType": "fact",
        "sourceType": "user",
        "state": "current",
    }
    request = seen[0]
    assert request.url.path == "/runtime/memory-upkeep"
    assert request.headers["x-memorygate-key"] == "read-private"
    assert request.headers["x-agent-id"] == "companion"
    assert dict(request.url.params) == {"unused_days": "90", "limit": "50"}


def test_an_older_memorygate_without_words_still_answers():
    reply = _reply(memories=False, unused=_kind({"ids": ["m1"], "reason": "never recalled"}))
    client, _ = _app(lambda request: httpx.Response(200, json=reply))
    body = client.get("/memory/upkeep").json()
    assert body["notRecalled"]["items"][0]["ids"] == ["m1"] and body["memories"] == {}


def test_openapi_names_the_reply_model():
    client, _ = _app(lambda request: httpx.Response(200, json=_reply()))
    operation = client.get("/openapi.json").json()["paths"]["/memory/upkeep"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/UpkeepPage"}


def test_settings_reach_memorygate_and_bad_ones_are_refused_first():
    client, seen = _app(lambda request: httpx.Response(200, json=_reply()))
    assert client.get("/memory/upkeep?unused_days=30&limit=5").status_code == 200
    assert dict(seen[0].url.params) == {"unused_days": "30", "limit": "5"}
    for query in ("unused_days=0", "unused_days=3651", "limit=0", "limit=501", "limit=many"):
        assert client.get("/memory/upkeep?" + query).status_code == 422
    assert len(seen) == 1


def test_a_refusal_from_memorygate_stays_a_422():
    client, _ = _app(lambda request: httpx.Response(422, json={"detail": "bad"}))
    response = client.get("/memory/upkeep")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"


def test_owner_session_is_required():
    client, seen = _app(lambda request: httpx.Response(200, json=_reply()), owner=False)
    assert client.get("/memory/upkeep").status_code == 401
    assert seen == []
    assert owner_allowed("GET", "/memory/upkeep")
    assert not owner_allowed("POST", "/memory/upkeep")


def test_a_candidate_naming_a_memory_being_forgotten_is_left_out_whole(tmp_path):
    store = Store(tmp_path / "pi.db")
    _forget(store, "m2", "pending")
    _forget(store, "m5", "forgotten")
    _forget(store, "m6", "rejected")
    reply = _reply(
        exact=_kind(
            {"ids": ["m1", "m2"], "reason": "same text"},
            {"ids": ["m3", "m4"], "reason": "same text"},
            total=7,
        ),
        expired=_kind({"ids": ["m5"], "reason": "valid until 2026-10-01"}),
        unused=_kind({"ids": ["m6"], "reason": "never recalled"}),
    )
    client, _ = _app(lambda request: httpx.Response(200, json=reply), store=store)
    body = client.get("/memory/upkeep").json()
    assert [item["ids"] for item in body["exactDuplicates"]["items"]] == [["m3", "m4"]]
    assert body["exactDuplicates"]["total"] == 6
    assert body["expired"] == {"items": [], "total": 0}
    assert [item["ids"] for item in body["notRecalled"]["items"]] == [["m6"]]
    # Neither half of a dropped pair keeps its words in the reply.
    assert set(body["memories"]) == {"m3", "m4", "m6"}


def test_a_memory_from_a_hidden_chat_is_left_out(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    monkeypatch.setattr(
        memory_beliefs, "hidden_objects", lambda db, agents: {"memory": {"m-hidden"}}
    )
    reply = _reply(
        near=_kind({"ids": ["m-hidden", "m-seen"], "reason": "86% of their words are the same"}),
        contradictions=_kind({"ids": ["m-a", "m-b"], "conflict_id": "c1", "reason": "differ"}),
    )
    client, _ = _app(lambda request: httpx.Response(200, json=reply), store=store)
    body = client.get("/memory/upkeep").json()
    assert body["nearDuplicates"] == {"items": [], "total": 0}
    assert [item["ids"] for item in body["contradictions"]["items"]] == [["m-a", "m-b"]]
    assert "m-hidden" not in body["memories"] and "m-seen" not in body["memories"]
    assert "words of m-hidden" not in client.get("/memory/upkeep").text


def test_unconfigured_or_silent_memory_is_503_never_an_empty_list():
    client, _ = _app(lambda request: httpx.Response(200, json=_reply()), configured=False)
    response = client.get("/memory/upkeep")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "memory_unavailable"

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    for upstream in (
        down,
        lambda request: httpx.Response(500, json={"detail": "private upstream body"}),
        lambda request: httpx.Response(200, json=_reply(agent="someone-else")),
        lambda request: httpx.Response(200, json={"agent_id": "companion", "expired": "nope"}),
        lambda request: httpx.Response(200, json={**_reply(), "memories": ["not", "a", "map"]}),
        lambda request: httpx.Response(200, content=b"not json"),
    ):
        client, _ = _app(upstream)
        response = client.get("/memory/upkeep")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "memory_unavailable"
        assert "private upstream" not in response.text
