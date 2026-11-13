"""Recall saved before a chat was put in the Trash is checked again on every use, and a chat in
the Trash opened by its own id still recalls its own records (pi.conversation.hidden_records).

Chat B recalls a memory written from chat C; the owner then trashes C. B's next turn (start-of-turn
recall and the saved `conker.recall` result) and a retry of B's answer carry no C text; restoring C
brings it back. A turn inside trashed chat H keeps H's own memories.
"""

import json
from contextlib import closing

import pytest
from test_tool_turns import FakeGate, build

from pi.context import controls as context_controls
from pi.conversation import archive, hidden_records
from pi.conversation.store import Store
from pi.memory import service
from pi.turns import context as turn_context
from pi.turns import recall

SECRET = "locker code is 4417"


class Client:
    agent_id = "companion-bound"

    def __init__(self, memories):
        self.memories = memories

    def retrieve(self, query, **options):
        return {"memories": list(self.memories), "retrieval": {"semantic": {"status": "ok"}}}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, session, trashed=True):
    archive.trash(store, archive.Trash(sessions=[session], trashed=trashed))


def written_in(store, session):
    """A memory MemoryGate made from one of `session`'s messages, citing it. Only the memory
    carries SECRET, so finding SECRET in a model input means the memory reached it."""
    message = store.append_message(session, "user", "a note about my locker")["id"]
    return {
        "id": "mem_" + session[-6:],
        "text": f"The owner's {SECRET}.",
        "source_type": "user",
        "citations": [{"session_id": session, "message_id": message}],
    }


def recall_call():
    return json.dumps({"tool": recall.RECALL, "args": {"query": "locker"}})


def model_input(messages):
    return "\n".join(m.content for m in messages)


def run(store, session, memories, replies):
    loop, provider = build(store, replies, FakeGate())
    loop.memory = service.Memory(store, Client(memories))
    result = loop.run_turn(session, "What is my locker code?")
    return result, provider


def refused_retry(store, turn_id):
    with pytest.raises(context_controls.ContextError) as failure:
        turn_context.replay(store, turn_id)
    return failure.value.detail["code"]


def test_trashing_the_source_chat_takes_its_memory_out_of_b_and_restore_brings_it_back(store):
    source, chat = store.create_session(title="C"), store.create_session(title="B")
    memory = written_in(store, source)
    first, provider = run(store, chat, [memory], [recall_call(), "It is 4417."])
    assert SECRET in model_input(provider.sent[0])
    assert SECRET in model_input(provider.sent[-1])
    assert SECRET in model_input(turn_context.replay(store, first["turn_id"])["messages"])

    trash(store, source)
    _, later = run(store, chat, [memory], ["I do not know."])
    seen = model_input(later.sent[-1])
    assert SECRET not in seen
    assert recall.CLEARED in seen
    assert refused_retry(store, first["turn_id"]) == "retry_source_unavailable"

    trash(store, source, trashed=False)
    _, restored = run(store, chat, [memory], ["It is 4417."])
    assert model_input(restored.sent[-1]).count(SECRET) >= 2
    assert SECRET in model_input(turn_context.replay(store, first["turn_id"])["messages"])


def test_a_saved_search_keeps_what_is_still_visible_and_says_what_was_left_out(store):
    source, other, chat = (store.create_session(title=t) for t in ("C", "D", "B"))
    hidden, kept = written_in(store, source), written_in(store, other)
    kept = {**kept, "text": "The owner's bike is blue."}
    run(store, chat, [], [recall_call(), "ok"])
    [row] = [m for m in store.messages(chat) if m["role"] == "tool"]
    with store._connect() as db:
        db.execute(
            "UPDATE recall_results SET package=? WHERE session_id=?",
            (json.dumps({"memories": [hidden, kept]}), chat),
        )
    trash(store, source)
    shown = json.loads(recall.expand(store, "tool", row["content"]))["result"]
    assert [item["id"] for item in shown["memories"]] == [kept["id"]]
    assert shown["count"] == 1 and shown["leftOut"] == recall.LEFT_OUT
    trash(store, source, trashed=False)
    shown = json.loads(recall.expand(store, "tool", row["content"]))["result"]
    assert [item["id"] for item in shown["memories"]] == [hidden["id"], kept["id"]]
    assert "leftOut" not in shown


def test_a_turn_inside_a_trashed_chat_keeps_its_own_memories(store):
    chat, elsewhere = store.create_session(title="H"), store.create_session(title="C")
    own, foreign = written_in(store, chat), written_in(store, elsewhere)
    foreign = {**foreign, "text": "The owner's PIN is 9921."}
    trash(store, chat)
    trash(store, elsewhere)
    result, provider = run(store, chat, [own, foreign], [recall_call(), "It is 4417."])
    seen = model_input(provider.sent[0]), model_input(provider.sent[-1])
    assert all(SECRET in text and "9921" not in text for text in seen)
    assert recall.CLEARED not in seen[-1]
    retry = model_input(turn_context.replay(store, result["turn_id"])["messages"])
    assert SECRET in retry and "9921" not in retry


def test_own_chat_counts_as_visible_only_while_it_is_not_forgotten(store):
    chat = store.create_session(title="H")
    message = store.append_message(chat, "user", "hello")["id"]
    _, _, derived = hidden_records.receipt_ids("a", message)
    trash(store, chat)
    with store._connect() as db:
        assert derived in hidden_records.hidden_records(db, ("a",))["memory"]
        assert derived not in hidden_records.hidden_records(db, ("a",), own=chat)["memory"]
        db.execute("UPDATE sessions SET status='forgotten' WHERE id=?", (chat,))
        db.commit()
        assert derived in hidden_records.hidden_records(db, ("a",), own=chat)["memory"]


def test_a_team_role_has_an_own_chat_only_when_it_reads_its_own(store):
    turn = {"session_id": "role-chat"}
    assert service._own_chat(turn, {"kind": "companion"}) == "role-chat"
    role = {"kind": "team-role", "memoryRead": {"sourceSessionId": "source-chat"}}
    assert service._own_chat(turn, role) is None
    role = {"kind": "team-role", "memoryRead": {"sourceSessionId": "role-chat"}}
    assert service._own_chat(turn, role) == "role-chat"
