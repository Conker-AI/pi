"""Search shows nothing from a forgotten or trashed chat, and a restored chat comes back.

Covers pi.conversation.search and pi.context.universal_search against the one visibility rule
(pi.conversation.visibility): chats, tasks, activity, artifacts, memories (library and meaning
search) and agents or AI jobs Conker kept from a hidden chat.
"""

from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi.agents import profiles as agents
from pi.content import artifact_models, artifacts
from pi.context import universal_search as search
from pi.conversation import archive
from pi.conversation import search as conversation_search
from pi.conversation import settings as session_settings
from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store
from pi.schedule import agent_jobs, job_models, tasks

AGENT = "companion"
NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, sessions, trashed=True):
    return archive.trash(store, archive.Trash(sessions=sessions, trashed=trashed))


def found(store, sources, query="needle", stage="metadata", memory=None):
    rows, _ = search.lexical(
        store, SimpleNamespace(client=memory), None, query, {"sources": sources}, stage
    )
    return {row["recordId"] for row in rows}


def derived(message_id, agent=AGENT):
    receipt = str(uuid5(NAMESPACE_URL, f"pi:{agent}:{message_id}"))
    return {
        "evidence": receipt,
        "analysis": str(uuid5(NAMESPACE_URL, receipt + ":analysis")),
        "memory": str(uuid5(NAMESPACE_URL, receipt + ":memory")),
    }


def keep(store, session_id, kind, target):
    with store._connect() as db:
        db.execute(
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES (?,?,?,?,0)",
            (f"req_{kind}_{target}", session_id, kind, target),
        )


def test_conversation_search_leaves_out_trashed_and_forgotten_chats(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        kept, binned, gone = (store.create_session(title=f"needle {n}") for n in "abc")
        for sid in (kept, binned, gone):
            store.append_message(sid, "user", "pack the needle")
        trash(store, [binned])
    forget(path, gone, preview(path, gone)["confirmation"])
    with closing(Store(path)) as store:
        rows = conversation_search.search(store, "needle")["results"]
        assert {row["session_id"] for row in rows} == {kept}
        assert {r["recordId"] for r in search.conversation_metadata(store, "needle")[0]} == {kept}
        assert not search.public_session(store, gone)
        assert not search.public_session(store, binned)
        trash(store, [binned], trashed=False)
        rows = conversation_search.search(store, "needle")["results"]
        assert {row["session_id"] for row in rows} == {kept, binned}
        assert search.public_session(store, binned)


def test_tasks_and_activity_of_a_trashed_chat_are_left_out(store):
    kept, binned = (store.create_session(title=t) for t in ("kept", "binned"))
    made = {
        sid: tasks.create(
            store,
            tasks.CreateTask(
                request_id=f"visibility_task_{sid}",
                session_id=sid,
                outcome="Needle outcome",
                criteria=["Done"],
            ),
        )["id"]
        for sid in (kept, binned)
    }
    trash(store, [binned])
    assert found(store, ["tasks"]) == {made[kept]}
    with store._connect() as db:
        events = dict(db.execute("SELECT id,session_id FROM activity_events").fetchall())
    shown = found(store, ["activity"], query="created")
    assert shown and all(events[e] == kept for e in shown)
    trash(store, [binned], trashed=False)
    assert found(store, ["tasks"]) == set(made.values())
    assert {events[e] for e in found(store, ["activity"], query="created")} == {kept, binned}


def test_artifacts_from_a_trashed_chat_are_left_out_until_restored(store):
    kept, binned = (store.create_session(title=t) for t in ("kept", "binned"))
    made = {}
    for sid in (kept, binned):
        message = store.complete_turn(store.start_turn(sid), "Needle answer")["id"]
        made[sid] = artifacts.create(
            store,
            artifact_models.FromMessage(title="Needle copy", sessionId=sid, messageId=message),
            resolve=session_settings.source_privacy,
        )["id"]
    own = artifacts.create(
        store,
        artifact_models.Create(
            title="Needle own", content={"kind": "markdown", "text": "Needle own text"}
        ),
    )["id"]
    trash(store, [binned])
    for stage in ("metadata", "text"):
        assert found(store, ["artifacts"], stage=stage) == {made[kept], own}
    trash(store, [binned], trashed=False)
    assert found(store, ["artifacts"]) == {made[kept], made[binned], own}


def test_agents_and_ai_jobs_kept_from_a_hidden_chat_are_not_listed(store):
    kept, binned = (store.create_session(title=t) for t in ("kept", "binned"))

    def agent(name):
        return agents.create(
            store,
            agents.AgentInput(
                name=name,
                role="Needle role",
                instructions="Needle instructions",
                modelId=None,
                toolIds=[],
                memory=agents.MemorySelection(scope="none", memoryIds=[]),
            ),
        )["id"]

    def job(name):
        body = job_models.AgentJobCreate.model_validate(
            dict(
                name=name,
                instructions="Look for needles.",
                timing=dict(kind="interval", time="00:00", day=0, hours=2),
                timeZone="Asia/Jerusalem",
            )
        )
        return agent_jobs.create(store, body, now=NOON)["id"]

    agent_ids = {"kept": agent("Needle kept"), "binned": agent("Needle binned")}
    job_ids = {"kept": job("Needle kept"), "binned": job("Needle binned")}
    keep(store, kept, "agent", agent_ids["kept"])
    keep(store, binned, "agent", agent_ids["binned"])
    keep(store, kept, "ai_job", job_ids["kept"])
    keep(store, binned, "ai_job", job_ids["binned"])
    trash(store, [binned])
    assert agent_ids["binned"] not in found(store, ["agents"])
    assert agent_ids["kept"] in found(store, ["agents"])
    assert found(store, ["jobs"]) == {job_ids["kept"]}
    trash(store, [binned], trashed=False)
    assert set(agent_ids.values()) <= found(store, ["agents"])
    assert found(store, ["jobs"]) == set(job_ids.values())


class Library:
    """MemoryGate's library: cards with a type and id, never the chat they came from."""

    agent_id = AGENT

    def __init__(self, cards):
        self.cards = cards

    def inspect(self, operation, payload):
        assert operation == "library"
        return {"scope": "all", "objects": self.cards, "next_after": None}


def test_memory_library_leaves_out_records_from_a_hidden_chat(store):
    kept, binned = (store.create_session(title=t) for t in ("kept", "binned"))
    said = {sid: store.append_message(sid, "user", "my needle")["id"] for sid in (kept, binned)}
    other = store.append_message(binned, "user", "sent elsewhere")["id"]
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET state='sent',destination_agent_id='other-agent' "
            "WHERE message_id=?",
            (other,),
        )
    keep(store, binned, "memory", "mem_kept_by_conker")
    hidden_ids = derived(said[binned])
    cards = [
        {"type": kind, "id": identity, "title": "Needle", "preview": ""}
        for kind, identity in [
            *hidden_ids.items(),
            ("memory", derived(other, "other-agent")["memory"]),
            ("memory", "mem_kept_by_conker"),
            ("memory", derived(said[kept])["memory"]),
            ("entity", "ent_unrelated"),
        ]
    ]
    memory = Library(cards)
    visible = {derived(said[kept])["memory"], "ent_unrelated"}
    trash(store, [binned])
    assert found(store, ["memory"], memory=memory) == visible
    trash(store, [binned], trashed=False)
    assert found(store, ["memory"], memory=memory) == {card["id"] for card in cards}


def client_for(store, memory):
    app = FastAPI()
    app.include_router(
        search.router(
            lambda: store,
            lambda: SimpleNamespace(client=memory),
            lambda: None,
            lambda: {},
            lambda: None,
        )
    )
    return TestClient(app)


def test_meaning_search_leaves_out_memories_of_a_hidden_chat(store):
    kept, binned = (store.create_session(title=t) for t in ("kept", "binned"))
    said = {sid: store.append_message(sid, "user", "my needle")["id"] for sid in (kept, binned)}
    keep(store, binned, "memory", "mem_kept_by_conker")
    memories = [
        {"id": "by-session", "summary": "Needle", "citations": [{"session_id": binned}]},
        {"id": "by-message", "summary": "Needle", "citations": [{"message_id": said[binned]}]},
        {"id": derived(said[binned])["memory"], "summary": "Needle"},
        {"id": "mem_kept_by_conker", "summary": "Needle"},
        {"id": "odd", "summary": "Needle", "citations": [{"session_id": [binned]}]},
        {"id": "visible", "summary": "Needle", "citations": [{"session_id": kept}]},
    ]

    class Memory:
        agent_id = AGENT

        def retrieve(self, query):
            return {"retrieval": {"mode": "semantic"}, "memories": memories}

    cfg = search.Settings(sources=["memory"], semantic=True)
    search.save_settings(
        store,
        search.SaveSettings(expected_revision=0, configuration=cfg),
        semantic_available=True,
        ranking_available=False,
    )
    client = client_for(store, Memory())

    def shown():
        page = client.get("/search", params={"q": "needle", "stage": "semantic"}).json()
        assert page["coverage"] == [{"source": "memory", "status": "searched"}]
        return {row["recordId"] for row in page["results"]}

    trash(store, [binned])
    assert shown() == {"odd", "visible"}  # malformed citations hide nothing
    trash(store, [binned], trashed=False)
    assert shown() == {item["id"] for item in memories}


def propose(store, session_id, memory_id, state):
    with store._connect() as db:
        db.execute(
            "INSERT INTO memory_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,0)",
            (
                f"mp_{memory_id}",
                session_id,
                "h",
                memory_id,
                AGENT,
                1,
                None,
                "Needle edit",
                None,
                "basis",
                "[]",
                state,
            ),
        )


def test_memories_a_hidden_chat_rewrote_are_left_out_until_restored(store):
    kept, binned = (store.create_session(title=t) for t in ("kept", "binned"))
    for memory_id, state in [
        ("mem_applied", "applied"),
        ("mem_applying", "applying"),
        ("mem_unknown", "unknown"),
        ("mem_pending", "pending"),
        ("mem_rejected", "rejected"),
    ]:
        propose(store, binned, memory_id, state)
    propose(store, kept, "mem_other_chat", "applied")
    ids = [
        "mem_applied",
        "mem_applying",
        "mem_unknown",
        "mem_pending",
        "mem_rejected",
        "mem_other_chat",
    ]
    memory = Library([{"type": "memory", "id": i, "title": "Needle", "preview": ""} for i in ids])
    memories = [{"id": i, "summary": "Needle"} for i in ids]
    visible = {"mem_pending", "mem_rejected", "mem_other_chat"}

    class Memory:
        agent_id = AGENT

        def retrieve(self, query):
            return {"retrieval": {"mode": "semantic"}, "memories": memories}

    trash(store, [binned])
    assert found(store, ["memory"], memory=memory) == visible
    assert {m["id"] for m in search.visible_memories(store, memories, (AGENT,))} == visible
    trash(store, [binned], trashed=False)
    assert found(store, ["memory"], memory=memory) == set(ids)
    assert {m["id"] for m in search.visible_memories(store, memories, (AGENT,))} == set(ids)


def test_hidden_records_match_the_memory_service_set(store):
    from pi.memory import hidden as service

    binned = store.create_session(title="binned")
    said = store.append_message(binned, "user", "my needle")["id"]
    keep(store, binned, "memory", "mem_kept_by_conker")
    propose(store, binned, "mem_applied", "applied")
    trash(store, [binned])
    with store._connect() as db:
        mine = search.hidden_records(db, (AGENT,))
        theirs = service.hidden_objects(db, (AGENT,))
    assert mine == {(kind, i) for kind, values in theirs.items() for i in values}
    assert ("memory", derived(said)["memory"]) in mine
