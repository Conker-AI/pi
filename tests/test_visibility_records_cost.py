"""What re-checking recall against hidden chats costs, and three gaps it had
(pi.conversation.hidden_records, pi.turns.recall, pi.agents.profiles, pi.agents.teams):

1. A history build works out the hidden records once, however many saved `conker.recall`
   results it holds, and a forgotten chat whose MemoryGate delete is done adds nothing to them
   (its memory and analysis are gone, its evidence invalidated), so a big forgotten chat costs
   nothing; a delete still owed keeps its records hidden.
2. A message sent before Pi pinned MemoryGate destinations (destination NULL) still hides the
   memory derived from it: for the reading agent in a history, and for every namespace Pi knows
   in a retry.
3. A name held by an agent or team Conker kept from a hidden chat is refused with a message that
   says so, instead of pointing at something no list shows.
"""

import json
import time
import uuid
from contextlib import closing

import pytest
from test_tool_turns import FakeGate, build
from test_visibility_kept_lists import make_agent, make_team

from pi.agents import profiles as agents
from pi.context import controls as context_controls
from pi.conversation import archive, hidden_records
from pi.conversation.store import Store
from pi.memory import service
from pi.turns import context as turn_context
from pi.turns import recall

READER = "companion-bound"


class Client:
    agent_id = READER

    def __init__(self, memories=()):
        self.memories = list(memories)

    def retrieve(self, query, **options):
        return {"memories": list(self.memories), "retrieval": {"semantic": {"status": "ok"}}}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, session, trashed=True):
    archive.trash(store, archive.Trash(sessions=[session], trashed=trashed))


def notes(store, session, count):
    """`count` owner messages in `session`, written at once (the outbox trigger queues each) and
    delivered to READER's MemoryGate namespace."""
    rows = [
        ("msg_" + uuid.uuid4().hex, session, index + 1, "user", json.dumps(f"note {index}"), 1.0)
        for index in range(count)
    ]
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?)", rows)
        db.execute(
            "UPDATE memory_outbox SET state='sent',delivery_started=1,destination_agent_id=? "
            "WHERE message_id IN (SELECT id FROM messages WHERE session_id=?)",
            (READER, session),
        )
        db.commit()
    return [row[0] for row in rows]


def saved_searches(store, chat, packages):
    """One saved `conker.recall` result per package, each with its record in `chat`."""
    with store._connect() as db:
        recall.initialize(db)
    for index, package in enumerate(packages):
        identity = f"rcl_{index}"
        with store._connect() as db:
            db.execute(
                "INSERT INTO recall_results VALUES (?,?,'turn_x',?,1.0)",
                (identity, chat, json.dumps(package)),
            )
            db.commit()
        record = {"tool": recall.RECALL, "result": {"status": "found", "recallId": identity}}
        store.append_message(chat, "tool", json.dumps(record))


def recalled(messages):
    """The memory ids each expanded recall result in a history shows."""
    shown = []
    for message in messages:
        if message.role == "tool" and recall.RECALL in message.content:
            result = json.loads(message.content)["result"]
            shown.append([item["id"] for item in result.get("memories", [])])
    return shown


def counting(monkeypatch, name):
    calls = []
    real = getattr(hidden_records, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(hidden_records, name, counted)
    return calls


def history(store, chat, memory=None):
    loop, _ = build(store, [], FakeGate())
    if memory is not None:
        loop.memory = memory
    return loop._history(chat)


def test_a_history_with_many_saved_searches_works_out_the_hidden_records_once(store, monkeypatch):
    hidden = store.create_session(title="T")
    [first, *_] = notes(store, hidden, 40)
    trash(store, hidden)
    _, _, derived = hidden_records.receipt_ids(READER, first)
    chat = store.create_session(title="B")
    results = 12
    saved_searches(store, chat, [{"memories": [{"id": derived}, {"id": "mem_ok"}]}] * results)
    calls = counting(monkeypatch, "_records")
    seen = recalled(history(store, chat, service.Memory(store, Client())))
    assert seen == [["mem_ok"]] * results
    assert len(calls) == 1
    # The next build reads again: a chat restored since is seen.
    trash(store, hidden, trashed=False)
    seen = recalled(history(store, chat, service.Memory(store, Client())))
    assert seen == [[derived, "mem_ok"]] * results
    assert len(calls) == 2


def test_a_big_forgotten_chat_whose_memorygate_delete_is_done_costs_nothing(store, monkeypatch):
    forgotten, binned = store.create_session(title="F"), store.create_session(title="T")
    gone = notes(store, forgotten, 3000)
    binned_notes = notes(store, binned, 5)
    with store._connect() as db:
        db.execute("UPDATE sessions SET status='forgotten' WHERE id=?", (forgotten,))
        db.execute(
            "UPDATE memory_outbox SET operation='delete',state='pending' "
            "WHERE message_id IN (SELECT id FROM messages WHERE session_id=?)",
            (forgotten,),
        )
        db.commit()
    trash(store, binned)
    owed = hidden_records.receipt_ids(READER, gone[0])[2]
    with store._connect() as db:
        # A delete still owed: its records stay hidden.
        assert owed in hidden_records.hidden_records(db, (READER,))["memory"]
        db.execute("UPDATE memory_outbox SET state='sent' WHERE operation='delete'")
        db.commit()
    chat = store.create_session(title="B")
    kept = hidden_records.receipt_ids(READER, binned_notes[0])[2]
    saved_searches(store, chat, [{"memories": [{"id": kept}, {"id": "mem_ok"}]}] * 20)
    loop, _ = build(store, [], FakeGate())
    loop.memory = service.Memory(store, Client())
    getattr(hidden_records.receipt_ids, "cache_clear", lambda: None)()
    derivations = counting(monkeypatch, "uuid5")
    started = time.perf_counter()
    seen = recalled(loop._history(chat))
    elapsed = time.perf_counter() - started
    assert seen == [["mem_ok"]] * 20
    # Only the Trash's five messages are derived (three ids each), once.
    assert len(derivations) == 3 * len(binned_notes)
    assert elapsed < 5.0


def legacy(store, session):
    """An owner message MemoryGate received before Pi pinned destinations: sent, no namespace."""
    message = store.append_message(session, "user", "my locker code")["id"]
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET state='sent',delivery_started=1,destination_agent_id=NULL "
            "WHERE message_id=?",
            (message,),
        )
        db.commit()
    return message


def test_an_unpinned_message_hides_its_memory_from_a_saved_search_in_history(store):
    source, chat = store.create_session(title="C"), store.create_session(title="B")
    derived = hidden_records.receipt_ids(READER, legacy(store, source))[2]
    saved_searches(store, chat, [{"memories": [{"id": derived}, {"id": "mem_ok"}]}])
    assert recalled(history(store, chat, service.Memory(store, Client()))) == [[derived, "mem_ok"]]
    trash(store, source)
    assert recalled(history(store, chat, service.Memory(store, Client()))) == [["mem_ok"]]


def test_an_unpinned_message_refuses_a_retry_that_recalled_its_memory(store):
    source, chat = store.create_session(title="C"), store.create_session(title="B")
    derived = hidden_records.receipt_ids(READER, legacy(store, source))[2]
    loop, _ = build(store, ["It is 4417."], FakeGate())
    loop.memory = service.Memory(store, Client([{"id": derived, "text": "locker 4417"}]))
    turn = loop.run_turn(chat, "What is my locker code?")["turn_id"]
    with store._connect() as db:
        # Pi has delivered to READER since (a pinned row), so it is a namespace Pi knows.
        db.execute(
            "UPDATE memory_outbox SET destination_agent_id=? WHERE message_id IN "
            "(SELECT id FROM messages WHERE session_id=?)",
            (READER, chat),
        )
        db.commit()
    assert turn_context.replay(store, turn)["messages"]
    trash(store, source)
    with pytest.raises(context_controls.ContextError) as refused:
        turn_context.replay(store, turn)
    assert refused.value.detail["code"] == "retry_source_unavailable"


def agent_body(name):
    return agents.AgentInput(
        name=name,
        role="role",
        instructions="Help.",
        modelId=None,
        toolIds=["read"],
        memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
    )


HIDDEN = "in a chat that is now forgotten or in the Trash"


def refusal(call):
    with pytest.raises(agents.AgentError) as refused:
        call()
    return refused.value.detail


def test_a_name_held_by_an_agent_kept_from_a_hidden_chat_is_refused_with_why(store):
    chat = store.create_session(title="H")
    make_agent(store, "Scout", chat)
    trash(store, chat)
    detail = refusal(lambda: agents.create(store, agent_body("scout")))
    assert detail["code"] == "name_conflict" and HIDDEN in detail["message"]
    trash(store, chat, trashed=False)
    detail = refusal(lambda: agents.create(store, agent_body("Scout")))
    assert detail["code"] == "name_conflict" and HIDDEN not in detail["message"]


def test_a_name_held_by_a_team_kept_from_a_hidden_chat_is_refused_with_why(store):
    chat = store.create_session(title="H")
    reader = make_agent(store, "Reader")
    make_team(store, "Crew", reader, chat)
    trash(store, chat)
    detail = refusal(lambda: make_team(store, "crew", reader))
    assert detail["code"] == "name_conflict" and HIDDEN in detail["message"]
    trash(store, chat, trashed=False)
    detail = refusal(lambda: make_team(store, "Crew", reader))
    assert detail["code"] == "name_conflict" and HIDDEN not in detail["message"]
