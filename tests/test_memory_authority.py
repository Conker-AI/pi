import json

import pytest

from pi import memory_authority, memory_store, session_settings
from pi.memory import Memory


def test_explicit_namespace_and_environment_key_only():
    result = memory_authority.clients(
        json.dumps({"agent_a": {"namespace": "research", "keyEnv": "READ_KEY"}}),
        {"READ_KEY": "synthetic"},
        lambda namespace, key: (namespace, key),
    )
    assert result == {"agent_a": ("research", "synthetic")}


@pytest.mark.parametrize(
    "raw",
    ['{"a":{},"a":{}}', '{"companion":{}}', '{"a":{"namespace":"x","keyEnv":"MISSING"}}', "[]"],
)
def test_invalid_binding_does_not_create_client(raw):
    with pytest.raises(ValueError, match="Invalid specialist"):
        memory_authority.clients(raw, {}, lambda *args: pytest.fail())


def test_specialist_never_falls_back_to_companion_and_privacy_stops_reads(monkeypatch):
    class Store:
        def get_turn(self, identity):
            return {"session_id": "session"}

    class Client:
        def __init__(self):
            self.calls = []

        def retrieve(self, query, **options):
            self.calls.append(options)
            return {"memories": [], "retrieval": {}}

    selected = {
        "kind": "agent",
        "agentId": "agent_a",
        "revision": 1,
        "privacy": {"memoryDisabled": False},
        "configuration": {"memory": {"scope": "selected", "memoryIds": ["memory_a"]}},
    }
    saved = []
    monkeypatch.setattr(session_settings, "execution", lambda *args: selected)
    monkeypatch.setattr(memory_store, "pending_deletions", lambda *args: False)
    monkeypatch.setattr(memory_store, "save_context", lambda *args: saved.append(args[2]))
    companion, specialist = Client(), Client()
    memory = Memory(Store(), companion)
    memory.prepare("turn", "query")
    assert saved[-1] == "not_configured" and not companion.calls
    memory.read_clients["agent_a"] = specialist
    memory.prepare("turn", "query")
    assert specialist.calls == [
        {"scope": "selected", "session_id": None, "memory_ids": ["memory_a"]}
    ]
    assert saved[-1] == "ok" and not companion.calls
    selected["privacy"]["memoryDisabled"] = True
    memory.prepare("turn", "query")
    assert saved[-1] == "disabled" and len(specialist.calls) == 1
