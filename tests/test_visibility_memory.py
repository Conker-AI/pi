"""Recall and the memory explorer leave out records from forgotten or trashed chats, and a
restored chat brings them back (pi.conversation.visibility)."""

import json
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi.agents import keep
from pi.api.routes.memory_explorer import router
from pi.conversation import archive
from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store
from pi.memory.service import MemoryClient, _receipt_ids, hidden_objects, without_hidden

AGENT = "companion-bound"


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, sessions, trashed=True):
    archive.trash(store, archive.Trash(sessions=sessions, trashed=trashed))


def kept_memory(store, session_id, memory_id, request_id="req-1"):
    """A memory Conker kept on its own from this chat (no citation reaches recall)."""
    with store._connect() as db:
        keep.record(
            db,
            {"kind": "memory", "id": request_id, "sessionId": session_id},
            {"status": "remembered", "memoryId": memory_id},
        )


def delivered(store, session_id, text="pack the tent"):
    """A message delivered to MemoryGate: its evidence, analysis and memory ids."""
    message = store.append_message(session_id, "user", text)["id"]
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET destination_agent_id=? WHERE message_id=?", (AGENT, message)
        )
    return message, _receipt_ids(AGENT, message)


def test_receipt_ids_match_memorygate_formula():
    # Same formula as delivery.validate_receipt and MemoryGate's conversation_memory.
    from uuid import NAMESPACE_URL, uuid5

    receipt = str(uuid5(NAMESPACE_URL, "pi:a:m"))
    assert _receipt_ids("a", "m") == (
        receipt,
        str(uuid5(NAMESPACE_URL, receipt + ":analysis")),
        str(uuid5(NAMESPACE_URL, receipt + ":memory")),
    )


def test_recall_drops_kept_and_derived_memories_of_a_trashed_chat(store):
    kept_chat, gone = (store.create_session(title=t) for t in ("kept", "gone"))
    kept_memory(store, gone, "mem-kept-from-gone")
    _, (evidence, _, memory) = delivered(store, gone)
    _, (other_evidence, _, other_memory) = delivered(store, kept_chat)
    package = {
        "memories": [
            {"id": "mem-kept-from-gone", "text": "likes tea"},
            {"id": memory, "text": "pack the tent"},
            {"id": other_memory, "text": "pack the tent"},
            {"id": "uncited", "text": "owner fact"},
        ],
        "evidence": [{"id": evidence}, {"id": other_evidence}],
        "retrieval": {},
    }
    assert without_hidden(store, package, (AGENT,)) == package
    trash(store, [gone])
    shown = without_hidden(store, package, (AGENT,))
    assert [m["id"] for m in shown["memories"]] == [other_memory, "uncited"]
    assert [e["id"] for e in shown["evidence"]] == [other_evidence]
    trash(store, [gone], trashed=False)
    assert without_hidden(store, package, (AGENT,)) == package


def test_recall_drops_records_citing_a_forgotten_chat(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        kept_chat, gone = (store.create_session(title=t) for t in ("kept", "gone"))
        kept_memory(store, gone, "mem-kept-from-gone")
    forget(path, gone, preview(path, gone)["confirmation"])
    package = {
        "memories": [
            {"id": "by-session", "citations": [{"session_id": gone}]},
            {"id": "mem-kept-from-gone"},
            {"id": "kept", "citations": [{"session_id": kept_chat}]},
        ],
        "retrieval": {},
    }
    with closing(Store(path)) as store:
        shown = without_hidden(store, package)
    assert [m["id"] for m in shown["memories"]] == ["kept"]


def test_hidden_objects_uses_the_reading_agent_when_no_destination_was_pinned(store):
    gone = store.create_session(title="gone")
    message = store.append_message(gone, "user", "pack the tent")["id"]
    trash(store, [gone])
    with store._connect() as db:
        found = hidden_objects(db, ("reader",))
    evidence, analysis, memory = _receipt_ids("reader", message)
    assert evidence in found["evidence"] and analysis in found["analysis"]
    assert memory in found["memory"]


def card(kind, identity):
    return {"type": kind, "id": identity, "preview": identity, "title": kind}


def explorer(store, answer):
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=answer(json.loads(request.content)))

    memory = MemoryClient(
        "http://memorygate", "ingest", "read", AGENT, transport=httpx.MockTransport(upstream)
    )
    app = FastAPI()
    app.include_router(router(lambda: SimpleNamespace(client=memory, store=store), lambda: None))
    return TestClient(app), seen, memory


def test_explorer_library_leaves_out_records_of_a_hidden_chat(store):
    kept_chat, gone = (store.create_session(title=t) for t in ("kept", "gone"))
    kept_memory(store, gone, "mem-kept-from-gone")
    _, (evidence, analysis, memory) = delivered(store, gone)
    _, (_, _, other_memory) = delivered(store, kept_chat)
    objects = [
        card("memory", "mem-kept-from-gone"),
        card("memory", memory),
        card("evidence", evidence),
        card("analysis", analysis),
        card("memory", other_memory),
        card("entity", memory),  # same id, other kind: not this chat's record
    ]
    client, _, mg = explorer(
        store, lambda body: {"scope": "all", "objects": objects, "total": 9, "next_after": None}
    )
    assert client.get("/memory/objects").json()["total"] == 9
    trash(store, [gone])
    body = client.get("/memory/objects").json()
    assert [(o["type"], o["id"]) for o in body["objects"]] == [
        ("memory", other_memory),
        ("entity", memory),
    ]
    assert body["total"] == 5
    trash(store, [gone], trashed=False)
    assert len(client.get("/memory/objects").json()["objects"]) == len(objects)
    mg.close()


def test_explorer_detail_refuses_a_hidden_record_and_drops_its_links(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        gone = store.create_session(title="gone")
        _, (evidence, analysis, memory) = delivered(store, gone)
    forget(path, gone, preview(path, gone)["confirmation"])
    link = {
        "id": "link:1",
        "source_type": "analysis",
        "source_id": analysis,
        "target_type": "memory",
        "target_id": "visible",
    }
    other = {**link, "id": "link:2", "source_type": "entity", "source_id": "e-1"}

    def answer(body):
        return {
            "scope": "all",
            "object": card(body["object_type"], body["object_id"]),
            "links": [link, other],
            "nodes": [card("analysis", analysis), card("entity", "e-1"), card("memory", "visible")],
        }

    with closing(Store(path)) as store:
        client, seen, mg = explorer(store, answer)
        for kind, identity in (("memory", memory), ("evidence", evidence)):
            response = client.get(f"/memory/objects/{kind}/{identity}?operation=content")
            assert response.status_code == 404
        assert seen == []  # refused before MemoryGate is asked
        body = client.get("/memory/objects/memory/visible").json()
        assert body["links"] == [other]
        assert [n["id"] for n in body["nodes"]] == ["e-1", "visible"]
        mg.close()


def proposal(store, session_id, memory_id, state, identity):
    """A memory proposal from this chat (pi.memory.proposals) in the given state."""
    with store._connect() as db:
        db.execute(
            "INSERT INTO memory_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (
                identity,
                session_id,
                "h",
                memory_id,
                AGENT,
                1,
                "{}",
                "new text",
                "why",
                "stated",
                "[]",
                state,
                1.0,
            ),
        )


def test_memory_rewritten_by_a_trashed_chats_applied_proposal_is_hidden(store):
    visible, gone = (store.create_session(title=t) for t in ("visible", "gone"))
    for identity, (chat, memory, state) in enumerate(
        (
            (gone, "mem-applied", "applied"),
            (gone, "mem-unknown", "unknown"),
            (gone, "mem-pending", "pending"),
            (gone, "mem-rejected", "rejected"),
            (visible, "mem-visible-edit", "applied"),
        )
    ):
        proposal(store, chat, memory, state, f"proposal-{identity:012d}")
    package = {
        "memories": [
            {"id": m}
            for m in (
                "mem-applied",
                "mem-unknown",
                "mem-pending",
                "mem-rejected",
                "mem-visible-edit",
            )
        ],
        "retrieval": {},
    }
    assert without_hidden(store, package, (AGENT,)) == package
    trash(store, [gone])
    with store._connect() as db:
        assert {"mem-applied", "mem-unknown"} <= hidden_objects(db, (AGENT,))["memory"]
    shown = without_hidden(store, package, (AGENT,))
    assert [m["id"] for m in shown["memories"]] == [
        "mem-pending",
        "mem-rejected",
        "mem-visible-edit",
    ]
    client, _, mg = explorer(
        store,
        lambda body: {"scope": "all", "objects": [card("memory", "mem-applied")], "total": 1},
    )
    assert client.get("/memory/objects").json()["objects"] == []
    assert client.get("/memory/objects/memory/mem-applied").status_code == 404
    trash(store, [gone], trashed=False)
    assert without_hidden(store, package, (AGENT,)) == package
    assert len(client.get("/memory/objects").json()["objects"]) == 1
    mg.close()
