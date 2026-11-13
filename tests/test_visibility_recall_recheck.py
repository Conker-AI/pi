"""Review follow-ups for re-checking saved results against hidden chats.

1. An agent call's reply is hidden only when the owner forgot or trashed the agent's chat (or the
   continuation that holds the reply), never because the child chat sits below a caller in the
   Trash: the caller opened by its own id still reads the reply (pi.agents.agent_calls.expand).
2. Re-checking a saved recall result costs no MemoryGate id derivation when nothing in it can be
   named by id, and a history with many saved results derives each hidden message's ids once
   (pi.conversation.hidden_records).
"""

import json

import pytest
from test_agent_calls import agent, call, chat, shown
from test_tool_turns import FakeGate, build

from pi.conversation import archive, hidden_records
from pi.conversation.store import Store
from pi.turns import recall

REPLY = "The secret plan."


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "pi.db")
    yield value
    value.close()


def trash(store, session, trashed=True):
    archive.trash(store, archive.Trash(sessions=[session], trashed=trashed))


def called(store):
    """A caller chat whose agent call came back with REPLY: (caller, child, call result)."""
    agent(store, "Scout")
    loop, _ = build(store, [REPLY], FakeGate())
    caller, turn = chat(store)
    result = call(loop, caller, turn, "Scout")
    assert shown(store, result)["reply"] == REPLY
    return caller, result.result["sessionId"], result


def test_a_caller_in_the_trash_still_reads_its_agents_reply_in_its_own_history(store):
    caller, child, result = called(store)
    trash(store, caller)
    seen = shown(store, result)
    assert seen["status"] == "done" and seen["reply"] == REPLY
    # The owner trashing the agent's own chat still takes the reply away, caller hidden or not.
    trash(store, child)
    assert shown(store, result)["status"] == "gone"
    trash(store, caller, trashed=False)
    assert shown(store, result)["status"] == "gone"
    trash(store, child, trashed=False)
    assert shown(store, result)["reply"] == REPLY


def test_a_forgotten_agent_chat_or_a_trashed_link_up_to_the_reply_hides_it(store):
    _, child, result = called(store)
    first = store.create_session(parent_id=child)
    second = store.create_session(parent_id=first)
    reply = store.append_message(second, "assistant", REPLY)["id"]
    with store._connect() as db:
        db.execute("UPDATE agent_calls SET reply_message_id=?", (reply,))
        db.commit()
    assert shown(store, result)["reply"] == REPLY
    # A continuation between the agent's chat and the one holding the reply.
    trash(store, first)
    assert shown(store, result)["status"] == "gone"
    trash(store, first, trashed=False)
    assert shown(store, result)["reply"] == REPLY
    with store._connect() as db:
        db.execute("UPDATE sessions SET status='forgotten' WHERE id=?", (child,))
        db.commit()
    assert shown(store, result)["status"] == "gone"


def test_a_reply_outside_the_agents_chat_tree_ignores_the_callers_trash(store):
    caller, _, result = called(store)
    stray = store.create_session(parent_id=caller)
    reply = store.append_message(stray, "assistant", REPLY)["id"]
    with store._connect() as db:
        db.execute("UPDATE agent_calls SET reply_message_id=?", (reply,))
        db.commit()
    trash(store, caller)
    assert shown(store, result)["reply"] == REPLY
    trash(store, stray)
    assert shown(store, result)["status"] == "gone"


def saved_search(store, chat, package):
    with store._connect() as db:
        recall.initialize(db)
        db.execute(
            "INSERT INTO recall_results VALUES ('rcl_1',?,'turn_x',?,1.0)",
            (chat, json.dumps(package)),
        )
        db.commit()
    return json.dumps({"tool": recall.RECALL, "result": {"status": "found", "recallId": "rcl_1"}})


def big_trash(store, messages=60):
    hidden = store.create_session(title="T")
    for index in range(messages):
        store.append_message(hidden, "user", f"note {index}")
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET destination_agent_id='companion'")
        db.commit()
    trash(store, hidden)
    return hidden


def count_derivations(monkeypatch):
    # A cache of the pure id formula may stay from other tests: start empty when there is one.
    getattr(hidden_records.receipt_ids, "cache_clear", lambda: None)()
    counted = []
    real = hidden_records.uuid5

    def counting(namespace, name):
        counted.append(name)
        return real(namespace, name)

    monkeypatch.setattr(hidden_records, "uuid5", counting)
    return counted


def test_a_saved_result_with_nothing_named_by_id_derives_no_ids(store, monkeypatch):
    big_trash(store)
    chat = store.create_session(title="B")
    text = saved_search(store, chat, {"entities": [{"id": "ent_1", "name": "Rowan"}]})
    counted = count_derivations(monkeypatch)
    found = json.loads(recall.expand(store, "tool", text))["result"]
    assert found["entities"] == [{"id": "ent_1", "name": "Rowan"}]
    assert counted == []


def test_many_saved_results_derive_each_hidden_messages_ids_once(store, monkeypatch):
    messages = 60
    big_trash(store, messages)
    chat = store.create_session(title="B")
    text = saved_search(store, chat, {"memories": [{"id": "mem_visible", "text": "kept"}]})
    counted = count_derivations(monkeypatch)
    for _ in range(20):
        found = json.loads(recall.expand(store, "tool", text))["result"]
        assert [item["id"] for item in found["memories"]] == ["mem_visible"]
    assert 0 < len(counted) <= 3 * messages


def test_a_cached_derivation_still_hides_a_derived_memory(store):
    hidden = big_trash(store, 3)
    [first, *_] = [m["id"] for m in store.messages(hidden)]
    _, _, memory = hidden_records.receipt_ids("companion", first)
    chat = store.create_session(title="B")
    text = saved_search(store, chat, {"memories": [{"id": memory}, {"id": "mem_visible"}]})
    for _ in range(2):
        found = json.loads(recall.expand(store, "tool", text))["result"]
        assert [item["id"] for item in found["memories"]] == ["mem_visible"]
    trash(store, hidden, trashed=False)
    found = json.loads(recall.expand(store, "tool", text))["result"]
    assert [item["id"] for item in found["memories"]] == [memory, "mem_visible"]
