"""Inbox gaps found while mapping merge 3 (docs/inbox-kinds.md): each one fails closed now."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from test_tool_turns import FakeGate, build

from pi.agents import asks, change_requests
from pi.api import server as api
from pi.conversation import archive
from pi.conversation.store import Store
from pi.turns import access, answers

OWNER = "inbox-hardening-owner-key-" + "o" * 32


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    monkeypatch.setattr(answers, "background", lambda work: work())


def memory_request(store, session):
    return change_requests.file(
        store, "memory", "Remember the gate code", "", {"text": "x"}, session_id=session
    )


def trash(store, session, trashed=True):
    archive.trash(store, archive.Trash(sessions=[session], trashed=trashed))


def test_a_request_from_a_trashed_chat_is_not_listed_or_answerable(store, monkeypatch):
    session = store.create_session()
    request = memory_request(store, session)
    owners = change_requests.file(store, "memory", "Mine", "", {"text": "y"})
    trash(store, session)
    assert [item["id"] for item in change_requests.listing(store)] == [owners["id"]]
    assert [item["id"] for item in change_requests.needs_you(store)] == [owners["id"]]
    with pytest.raises(change_requests.ChangeRequestError) as hidden:
        change_requests.shown(store, request["id"])
    assert hidden.value.status == 404

    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(api.app.state, "toolgate", None, raising=False)
    client = TestClient(api.app)
    owner = {"X-Pi-Owner-Key": OWNER}
    path = f"/change-requests/{request['id']}"
    assert client.get(path, headers=owner).status_code == 404
    for verb in ("approve", "decline", "retry", "saved", "published"):
        assert client.post(f"{path}/{verb}", headers=owner, json={}).status_code in (404, 422)
    assert client.post(f"{path}/approve", headers=owner).status_code == 404
    assert client.post(f"{path}/decline", headers=owner).status_code == 404
    assert change_requests.get(store, request["id"])["state"] == "open"
    # Restoring the chat brings it back, undecided.
    trash(store, session, trashed=False)
    assert change_requests.shown(store, request["id"])["state"] == "open"
    assert client.get(path, headers=owner).status_code == 200


def test_hidden_requests_do_not_fill_the_overall_limit(store):
    per_chat = change_requests.PER_CHAT
    for _ in range(change_requests.OPEN_LIMIT // per_chat + 1):
        session = store.create_session()
        for _ in range(per_chat):
            memory_request(store, session)
        trash(store, session)
    assert memory_request(store, store.create_session())["state"] == "open"


def test_an_unknown_kind_is_never_made_as_a_memory(store, monkeypatch):
    request = memory_request(store, None)
    real = change_requests.get
    monkeypatch.setattr(change_requests, "get", lambda *args: {**real(*args), "kind": "question"})
    stored = []
    with pytest.raises(change_requests.ChangeRequestError) as refused:
        change_requests.approve(store, request["id"], memory=lambda *a, **k: stored.append(a))
    assert refused.value.detail["code"] == "unknown_kind" and stored == []


def test_an_option_sent_without_the_flag_is_still_the_ais_words(store):
    loop, _ = build(store, ["Will do."], FakeGate())
    session = store.create_session()
    question = asks.file(store, session, None, "Send Dani the notes?", ["Yes", "No"])
    answered = asks.answer(store, question["id"], "Yes", chosen=False)
    assert answered["chosen"] is True
    answers.answered(loop, answered)
    with store._connect() as db:
        newest = db.execute("SELECT id FROM turns WHERE session_id=?", (session,)).fetchone()["id"]
    assert access.owner_text(store, newest) == ""


def test_an_answer_turn_whose_link_is_gone_fails_closed(store):
    loop, _ = build(store, ["Will do."], FakeGate())
    session = store.create_session()
    question = asks.file(store, session, None, "Send Dani the notes?", ["Yes", "No"])
    answers.answered(loop, asks.answer(store, question["id"], "Yes, and copy Sam", chosen=False))
    with store._connect() as db:
        newest = db.execute("SELECT id FROM turns WHERE session_id=?", (session,)).fetchone()["id"]
        db.execute("UPDATE owner_asks SET answer_request=NULL")
        db.commit()
    # The message holds the AI's question: with the link gone, none of it is his request.
    assert access.owner_text(store, newest) == ""
    assert access.outside_content(store, session, "", lambda tool: False, turn_id=newest)
