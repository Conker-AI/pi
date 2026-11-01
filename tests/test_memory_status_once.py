"""Memory status optimizations: delivery is read once per session, not once per turn."""

import json
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory.service import Memory, MemoryClient
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.turns.loop import Loop


class Provider:
    name = "test"

    def __init__(self):
        self.calls = []

    def complete(self, messages, *, model):
        self.calls.append(messages)
        return Completion(
            text="A plain reply with no memory warning.", provider=self.name, model=model
        )


def client(handler):
    return MemoryClient(
        "http://memory.test",
        "ingest-secret-long",
        "read-secret-long",
        transport=httpx.MockTransport(handler),
    )


def test_statuses_match_status_for_every_turn(tmp_path):
    store = Store(tmp_path / "pi.db")
    session = store.create_session()

    turn1 = store.start_turn(session)
    turn2 = store.start_turn(session)
    turn3 = store.start_turn(session)

    memory_store.save_context(store, turn1, "ok", {"memories": [{"text": "Memory 1"}]})
    memory_store.save_context(store, turn2, "unavailable")

    with store._connect() as db:
        msg1 = store.append_message(session, "user", "Message 1")
        msg2 = store.append_message(session, "user", "Message 2")
        msg3 = store.append_message(session, "user", "Message 3")

        db.execute("UPDATE memory_outbox SET state='pending' WHERE message_id=?", (msg1["id"],))
        db.execute(
            "UPDATE memory_outbox SET state='blocked', error='Test error' WHERE message_id=?",
            (msg2["id"],),
        )
        db.execute(
            "UPDATE memory_outbox SET state='sent', receipt=? WHERE message_id=?",
            (
                json.dumps(
                    {
                        "id": str(uuid5(NAMESPACE_URL, f"pi:default:{msg3['id']}")),
                        "message_id": msg3["id"],
                        "state": "admitted",
                        "memory_id": str(uuid5(NAMESPACE_URL, f"pi:default:{msg3['id']}:memory")),
                    }
                ),
                msg3["id"],
            ),
        )

    turn_ids = [turn1, turn2, turn3]

    for configured in [False, True]:
        result = memory_store.statuses(store, session, turn_ids, configured=configured)

        session_status = memory_store.status(store, session, configured=configured)
        assert result["session"] == session_status

        for turn_id in turn_ids:
            turn_status = memory_store.status(store, session, turn_id, configured=configured)
            assert result["turns"][turn_id] == turn_status

    store.close()


def test_opening_a_chat_reads_delivery_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    memory = Memory(store, client(lambda request: httpx.Response(503)))
    loop = Loop(store, Router(local_provider=Provider(), local_model="test"), memory=memory)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "loop", loop, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "test-admin", raising=False)
    http = TestClient(api.app)

    session = store.create_session()
    for i in range(3):
        response = http.post(
            f"/sessions/{session}/turns",
            json={"text": f"Message {i + 1}"},
            headers={"X-Pi-Key": "test-admin"},
        )
        assert response.status_code == 200

    original_delivery = memory_store.delivery
    call_count = 0

    def counting_delivery(store, session_id=None):
        nonlocal call_count
        call_count += 1
        return original_delivery(store, session_id)

    monkeypatch.setattr(memory_store, "delivery", counting_delivery)

    response = http.get(f"/sessions/{session}", headers={"X-Pi-Key": "test-admin"})
    assert response.status_code == 200

    assert call_count == 1

    result = response.json()
    assert len(result["turns"]) == 3

    for turn in result["turns"]:
        turn_id = turn["id"]
        expected_status = memory_store.status(store, session, turn_id, configured=True)
        assert turn["memory"] == expected_status

    memory.close()
    store.close()
