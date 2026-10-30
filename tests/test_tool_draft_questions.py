"""The drafter's question goes to the chat that asked; the owner's answer goes on with the draft."""

import json

import pytest

from pi.agents import change_requests, tool_drafts
from pi.conversation.store import Store
from pi.turns import tool_drafting
from tests.test_tool_drafts import READY, WANTED, build, shown

QUESTION = "Which search engine?"
ASKED = json.dumps({"reply": QUESTION, "ready": False, "document": None})


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def asked(question=QUESTION):
    return tool_drafting.QUESTION.format(question=question)


def said(*answers):
    entries = []
    for answer in answers:
        entries += [{"role": "builder", "content": QUESTION}, {"role": "owner", "content": answer}]
    return entries


def test_a_question_for_a_chat_request_is_asked_in_that_chat(store):
    loop, _ = build(store, [ASKED])
    session = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    loop.drafts.tick()
    request = change_requests.get(store, filed["id"])
    assert request["state"] == "open"
    assert request["payload"] == {
        "description": WANTED,
        "status": "needs_detail",
        "question": QUESTION,
        "askedIn": session,
    }
    [note] = store.messages(session)
    assert note["role"] == "assistant" and note["content"] == asked()
    assert tool_drafts.pending(store) == []


def test_the_owners_next_message_there_goes_on_with_the_same_draft(store):
    loop, drafter = build(store, [ASKED, READY])
    session = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    loop.drafts.tick()
    loop.drafts._wake.clear()
    result = loop.run_turn(session, "Bing, please")
    # Conker's stored reply, no model call: the drafter was called once, for the question.
    assert result["message"]["content"] == tool_drafting.GOING_ON
    assert len(drafter.calls) == 1 and loop.drafts._wake.is_set()
    request = change_requests.get(store, filed["id"])
    assert request["payload"] == {
        "description": WANTED,
        "status": "drafting",
        "conversation": said("Bing, please"),
    }
    assert request["title"] == "Draft a tool: " + WANTED
    assert [(m["role"], m["content"]) for m in store.messages(session)] == [
        ("assistant", asked()),
        ("user", "Bing, please"),
        ("assistant", tool_drafting.GOING_ON),
    ]
    loop.drafts.tick()
    assert shown(drafter, 1)["conversation"] == said("Bing, please")
    payload = change_requests.get(store, filed["id"])["payload"]
    assert payload["status"] == "ready" and payload["conversation"] == said("Bing, please")


def test_a_message_in_another_chat_is_not_the_answer(store):
    loop, drafter = build(store, [ASKED, "Hello there."])
    session, other = store.create_session(), store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    loop.drafts.tick()
    result = loop.run_turn(other, "Hello")
    assert result["message"]["content"] == "Hello there." and len(drafter.calls) == 2
    assert change_requests.get(store, filed["id"])["payload"]["status"] == "needs_detail"


def test_the_question_follows_a_fork_and_the_answer_is_taken_there(store):
    loop, _ = build(store, [ASKED])
    parent = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=parent)
    store.close_session(parent, "forked")
    child = store.create_session(parent_id=parent)
    loop.drafts.tick()
    assert change_requests.get(store, filed["id"])["payload"]["askedIn"] == child
    assert store.messages(parent) == []
    assert loop.run_turn(child, "Bing")["message"]["content"] == tool_drafting.GOING_ON
    assert change_requests.get(store, filed["id"])["payload"]["status"] == "drafting"


def test_an_answer_from_the_inbox_goes_on_the_same_way(store):
    loop, drafter = build(store, [ASKED, READY, READY])
    session = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    loop.drafts.tick()
    again = tool_drafts.retry(store, filed["id"], tool_drafts.Retry(answer=" Bing "))
    assert again["payload"] == {
        "description": WANTED,
        "status": "drafting",
        "conversation": said("Bing"),
    }
    loop.drafts.tick()
    assert shown(drafter, 1)["conversation"] == said("Bing")
    with pytest.raises(tool_drafts.ToolDraftError) as nothing:
        tool_drafts.retry(store, filed["id"], tool_drafts.Retry(answer="Google"))
    assert nothing.value.code == "not_asking" and nothing.value.status == 409
    # Trying again as it was keeps his answers; a new description starts over.
    same = tool_drafts.retry(store, filed["id"], tool_drafts.Retry())
    assert same["payload"]["conversation"] == said("Bing")
    loop.drafts.tick()
    fresh = tool_drafts.retry(store, filed["id"], tool_drafts.Retry(description="Search Bing"))
    assert fresh["payload"] == {"description": "Search Bing", "status": "drafting"}
    for values in ({"description": "a", "answer": "b"}, {"answer": "  "}, {"answer": ""}):
        with pytest.raises(ValueError):
            tool_drafts.Retry(**values)


def test_the_drafter_asks_at_most_four_times(store):
    loop, _ = build(store, [ASKED] * (tool_drafts.QUESTIONS + 1))
    session = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    for number in range(tool_drafts.QUESTIONS):
        loop.drafts.tick()
        assert change_requests.get(store, filed["id"])["payload"]["status"] == "needs_detail"
        tool_drafts.retry(store, filed["id"], tool_drafts.Retry(answer=f"Answer {number}"))
    loop.drafts.tick()
    payload = change_requests.get(store, filed["id"])["payload"]
    assert payload["status"] == "failed"
    assert payload["error"] == "The drafter still has a question: " + QUESTION
    assert payload["conversation"] == said(*(f"Answer {n}" for n in range(4)))


def test_a_busy_chat_gets_the_question_once_free_unless_answered_first(store, monkeypatch):
    loop, _ = build(store, [ASKED, ASKED])
    session = store.create_session()
    turn = store.start_turn(session)
    later = []
    monkeypatch.setattr(tool_drafting.answers, "background", later.append)
    monkeypatch.setattr(tool_drafting, "BUSY_WAIT", 0)
    first = tool_drafts.file(store, WANTED, session_id=session)
    loop.drafts.tick()
    payload = change_requests.get(store, first["id"])["payload"]
    assert payload["status"] == "needs_detail" and "askedIn" not in payload
    # Not posted yet, so his message in the chat meanwhile is not taken as the answer.
    assert store.messages(session) == []
    store.finish_turn(turn, "complete")
    later.pop()()
    assert change_requests.get(store, first["id"])["payload"]["askedIn"] == session
    assert [m["content"] for m in store.messages(session)] == [asked()]
    # Answered from the Inbox before the chat was free: nothing is posted.
    second = tool_drafts.file(store, WANTED + " twice", session_id=session)
    turn = store.start_turn(session)
    loop.drafts.tick()
    tool_drafts.retry(store, second["id"], tool_drafts.Retry(answer="Bing"))
    store.finish_turn(turn, "complete")
    later.pop()()
    assert [m["content"] for m in store.messages(session)] == [asked()]


def test_a_question_not_posted_when_pi_stopped_is_asked_at_start(store):
    session = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    tool_drafts.file(store, "Without a chat")
    [request] = [item for item in tool_drafts.pending(store) if item["id"] == filed["id"]]
    tool_drafts.finish(store, request, tool_drafts.needs_detail(WANTED, QUESTION))
    assert tool_drafting.ask_waiting(store) == 1
    assert change_requests.get(store, filed["id"])["payload"]["askedIn"] == session
    assert [m["content"] for m in store.messages(session)] == [asked()]
    assert tool_drafting.ask_waiting(store) == 0


def test_a_restart_while_drafting_after_an_answer_keeps_the_answers(store):
    session = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    [request] = tool_drafts.pending(store)
    tool_drafts.finish(store, request, tool_drafts.needs_detail(WANTED, QUESTION))
    tool_drafts.retry(store, filed["id"], tool_drafts.Retry(answer="Bing"))
    assert tool_drafts.recover_interrupted(store) == 1
    payload = change_requests.get(store, filed["id"])["payload"]
    assert payload == tool_drafts.failed(WANTED, tool_drafts.RESTARTED, said("Bing"))
