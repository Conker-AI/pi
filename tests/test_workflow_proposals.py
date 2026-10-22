"""Workflow drafts made from the owner's chat messages: Inbox items that leave with the chat."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import inbox, workflow_proposals
from pi.api import server
from pi.conversation import forgetting
from pi.conversation.store import Store


@pytest.fixture
def chat(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        owner = store.append_message(session, "user", "Every morning check the weather and warn me")
        reply = store.append_message(session, "assistant", "I can draft that.")
        yield store, path, session, owner["id"], reply["id"]


def body(session, message, **values):
    return workflow_proposals.Record.model_validate(
        {
            "sessionId": session,
            "messageId": message,
            "draftId": "morning-weather",
            "name": "Morning weather",
            "valid": True,
            **values,
        }
    )


def test_a_draft_from_an_owner_message_waits_in_the_inbox(chat):
    store, _, session, owner, _ = chat
    item = workflow_proposals.record(store, body(session, owner), now=100.0)
    assert item["state"] == "open" and item["draftId"] == "morning-weather"
    workflow_proposals.record(
        store, body(session, owner, draftId="broken", name="Broken", valid=False)
    )
    titles = [
        entry["title"] for entry in inbox.inbox(store)["needsYou"] if entry["kind"] == "workflow"
    ]
    assert titles == ["Workflow draft ready: Morning weather", "Workflow draft to fix: Broken"]


def test_only_the_owners_own_messages_in_that_chat(chat):
    store, _, session, owner, reply = chat
    with pytest.raises(workflow_proposals.ProposalError) as assistant:
        workflow_proposals.record(store, body(session, reply))
    assert assistant.value.status == 409
    other = store.create_session()
    with pytest.raises(workflow_proposals.ProposalError) as elsewhere:
        workflow_proposals.record(store, body(other, owner))
    assert elsewhere.value.status == 404
    with pytest.raises(ValueError):
        body(session, owner, draftId="../escape")


def test_publishing_or_dismissing_clears_the_inbox(chat):
    store, _, session, owner, _ = chat
    first = workflow_proposals.record(store, body(session, owner))
    second = workflow_proposals.record(store, body(session, owner, draftId="other", name="Other"))
    closed = workflow_proposals.published(
        store, workflow_proposals.Published(draftId="morning-weather")
    )
    assert closed == {"draftId": "morning-weather", "closed": 1}
    assert workflow_proposals.dismiss(store, second["id"])["state"] == "dismissed"
    assert workflow_proposals.open_items(store) == []
    assert workflow_proposals.dismiss(store, first["id"])["state"] == "published"
    with pytest.raises(workflow_proposals.ProposalError) as missing:
        workflow_proposals.dismiss(store, "wfp_" + "0" * 32)
    assert missing.value.status == 404


def test_forgetting_the_chat_removes_its_drafts_record(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        owner = store.append_message(session, "user", "Every morning check the weather")["id"]
        workflow_proposals.record(store, body(session, owner))
    # Forgetting needs the database to itself, as when every Pi process is stopped.
    forgetting.forget(path, session, forgetting.preview(path, session)["confirmation"])
    with closing(Store(path)) as store:
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM workflow_proposals").fetchone()[0] == 0
        assert workflow_proposals.needs_you(store) == []
        with pytest.raises(workflow_proposals.ProposalError):
            workflow_proposals.record(store, body(session, owner))


def test_routes_need_the_admin_key(chat):
    store, _, session, owner, _ = chat
    app = server.app
    app.state.store = store
    app.state.admin_key = "test-admin-key"
    client = TestClient(app)
    admin = {"X-Pi-Key": "test-admin-key"}
    payload = {
        "sessionId": session,
        "messageId": owner,
        "draftId": "morning-weather",
        "name": "Morning weather",
        "valid": True,
    }
    assert client.post("/workflow-proposals", json=payload).status_code == 401
    created = client.post("/workflow-proposals", json=payload, headers=admin)
    assert created.status_code == 200, created.text
    assert len(client.get("/workflow-proposals", headers=admin).json()["items"]) == 1
    assert client.post("/workflow-proposals/x/dismiss", headers=admin).status_code == 422
    done = client.post(
        "/workflow-proposals/published", json={"draftId": "morning-weather"}, headers=admin
    )
    assert done.json()["closed"] == 1
