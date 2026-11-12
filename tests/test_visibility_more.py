"""Lists, Inbox, artifacts, retries and calls leave out a trashed chat, and restoring it brings
everything back (pi.conversation.visibility)."""

from contextlib import closing

import pytest

from pi.agents import inbox
from pi.calls import call_models, call_records, call_turns
from pi.calls import runtime as calls
from pi.content import artifacts
from pi.context import controls as context_controls
from pi.conversation import archive
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models.providers import Completion, Message
from pi.models.routing import Router
from pi.turns import access
from pi.turns import context as turn_context
from pi.turns.loop import Loop


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        with value._connect() as db:
            db.executescript(artifacts.SCHEMA)
        yield value


def trash(store, sessions, trashed=True):
    archive.trash(store, archive.Trash(sessions=sessions, trashed=trashed))


def test_session_list_leaves_out_a_trashed_chat(store):
    kept, gone = (store.create_session(title=t) for t in ("kept", "gone"))
    assert {s["id"] for s in store.list_sessions()} >= {kept, gone}
    trash(store, [gone])
    ids = {s["id"] for s in store.list_sessions()}
    assert kept in ids and gone not in ids
    trash(store, [gone], trashed=False)
    assert gone in {s["id"] for s in store.list_sessions()}


def test_handled_actions_leave_out_a_trashed_chat(store):
    kept, gone = (store.create_session(title=t) for t in ("kept", "gone"))
    for chat in (kept, gone):
        turn = store.start_turn(chat)
        access.record(
            store,
            session_id=chat,
            turn_id=turn,
            action_id=f"act-{chat}",
            tool_id="tool",
            decided_by="ai",
            reason="fine",
        )
    assert {h["sessionId"] for h in access.handled(store, 0)} == {kept, gone}
    trash(store, [gone])
    assert {h["sessionId"] for h in access.handled(store, 0)} == {kept}
    trash(store, [gone], trashed=False)
    assert {h["sessionId"] for h in access.handled(store, 0)} == {kept, gone}


def from_chat(store, session):
    turn = store.start_turn(session)
    message = store.complete_turn(turn, "an answer")
    body = artifacts.FromMessage(title="copy", sessionId=session, messageId=message["id"])
    return artifacts.create(store, body, session_settings.source_privacy)


def test_artifact_list_leaves_out_a_copy_from_a_trashed_chat(store):
    kept, gone = (store.create_session(title=t) for t in ("kept", "gone"))
    own = artifacts.create(
        store, artifacts.Create(title="own", content={"kind": "markdown", "text": "x"})
    )
    copies = [from_chat(store, chat)["id"] for chat in (kept, gone)]
    every = {own["id"], *copies}
    assert {a["id"] for a in artifacts.list_artifacts(store)} == every
    trash(store, [gone])
    assert {a["id"] for a in artifacts.list_artifacts(store)} == {own["id"], copies[0]}
    trash(store, [gone], trashed=False)
    assert {a["id"] for a in artifacts.list_artifacts(store)} == every


def test_inbox_upcoming_leaves_out_a_task_of_a_trashed_chat(store):
    gone = store.create_session(title="gone")
    task = "tsk_" + "b" * 32
    with store._connect() as db:
        db.execute(
            "INSERT INTO tasks(id,session_id,outcome,criteria,status,revision,"
            "created_at,updated_at) VALUES(?,?,'outcome','[]','planned',1,1,1)",
            (task, gone),
        )

    def upcoming():
        return [i["id"] for i in inbox.inbox(store)["upcoming"] if i["kind"] == "task"]

    assert upcoming() == [task]
    trash(store, [gone])
    assert upcoming() == []
    trash(store, [gone], trashed=False)
    assert upcoming() == [task]


def test_retry_refuses_a_line_inherited_from_a_trashed_chat(store):
    parent = store.create_session(title="parent")
    inherited = store.append_message(parent, "user", "an inherited line")
    child = store.create_session(parent_id=parent, summary="s")
    with store._connect() as db:
        db.execute("INSERT INTO context_inherited_messages VALUES(?,?,0)", (child, inherited["id"]))
    turn = store.start_turn(child)
    own = store.append_message(child, "user", "own", turn_id=turn, purpose="input")
    turn_context.capture(store, turn, [inherited, own], [Message("system", "prefix")])
    store.finish_turn(turn, "complete")
    assert len(turn_context.replay(store, turn)["messages"]) == 3
    trash(store, [parent])
    with pytest.raises(context_controls.ContextError) as refused:
        turn_context.replay(store, turn)
    assert refused.value.detail["code"] == "retry_source_unavailable"
    trash(store, [parent], trashed=False)
    assert len(turn_context.replay(store, turn)["messages"]) == 3


class Provider:
    name = "one"

    def complete(self, messages, *, model):
        return Completion(text="an answer", model=model, provider=self.name)

    def complete_bounded(self, messages, *, model, timeout):
        return self.complete(messages, model=model)


def test_call_stops_while_its_source_chat_is_in_the_trash(store):
    source = store.create_session(title="source")
    call = calls.start(
        store, call_models.Start(request_id="start_request_857301", conversationId=source)
    )
    loop = Loop(store, Router(local_provider=Provider(), local_model="fake"))

    def send(number):
        body = call_models.Send(request_id=f"call_request_8574_{number}", text="question")
        return call_turns.run(store, loop, call["id"], body)

    trash(store, [source])
    with pytest.raises(call_records.CallError, match="unavailable"):
        send(1)
    trash(store, [source], trashed=False)
    send(2)
