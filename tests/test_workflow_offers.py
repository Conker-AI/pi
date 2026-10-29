"""Offers Pi made before Conker drafted tools itself stay until accepted or dismissed; no new ones."""

import time
import uuid
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import inbox, workflow_offers, workflow_proposals
from pi.api import server
from pi.conversation import forgetting
from pi.conversation.store import Store
from pi.models.routing import Router
from pi.turns.loop import Loop
from tests.test_tool_turns import FakeGate, Scripted

WEATHER = "Every morning tell me the weather in Tel Aviv"


def build(tmp_path, replies, gate=None):
    store = Store(tmp_path / "pi.db")
    provider = Scripted(replies)
    loop = Loop(
        store,
        Router(local_provider=provider, local_model="test-model"),
        toolgate=FakeGate() if gate is None else gate,
    )
    return store, loop, provider


def offers_in(store):
    return [item for item in inbox.inbox(store)["needsYou"] if item["kind"] == "workflow-offer"]


@pytest.mark.parametrize("tools", [True, False])
def test_pi_makes_no_new_offers(tmp_path, tools):
    store, loop, provider = build(tmp_path, ["I can ask for that as a cron job."])
    if not tools:
        loop.toolgate = None
    loop.run_turn(store.create_session(), WEATHER)
    assert offers_in(store) == []
    system = [m.content for m in provider.sent[0] if m.role == "system"]
    assert not any("Nothing is drafted" in text for text in system)


def offer(store, session):
    """An offer as Pi used to make them, for the latest message in this chat."""
    with store._connect() as db:
        message = db.execute(
            "SELECT id FROM messages WHERE session_id=? AND role='user' ORDER BY seq DESC",
            (session,),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO workflow_offers VALUES (?,?,?,'open',?,NULL)",
            ("wfo_" + uuid.uuid4().hex, session, message, time.time()),
        )


def offered(tmp_path):
    store, loop, _ = build(tmp_path, ["Noted."])
    session = store.create_session()
    loop.run_turn(session, WEATHER)
    offer(store, session)
    [item] = offers_in(store)
    return store, session, item


def test_drafting_from_the_message_takes_the_offers_place(tmp_path):
    store, session, item = offered(tmp_path)
    workflow_proposals.record(
        store,
        workflow_proposals.Record(
            sessionId=session,
            messageId=item["source"]["messageId"],
            draftId="morning-weather",
            name="Morning weather",
            valid=True,
        ),
    )
    assert offers_in(store) == []
    assert workflow_offers.get(store, item["id"])["state"] == "accepted"
    kinds = [entry["kind"] for entry in inbox.inbox(store)["needsYou"]]
    assert "workflow" in kinds


def test_a_dismissed_offer_stays_dismissed(tmp_path):
    store, _, item = offered(tmp_path)
    assert workflow_offers.dismiss(store, item["id"])["state"] == "dismissed"
    assert offers_in(store) == []
    with pytest.raises(workflow_offers.OfferError):
        workflow_offers.dismiss(store, "wfo_" + "0" * 32)


def test_forgetting_the_chat_removes_its_offers(tmp_path):
    store, session, _ = offered(tmp_path)
    path = tmp_path / "pi.db"
    store.close()
    forgetting.forget(path, session, forgetting.preview(path, session)["confirmation"])
    with closing(Store(path)) as reopened, reopened._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_offers").fetchone()[0] == 0


def test_the_excerpt_is_one_short_line_without_control_characters():
    text = "Every\x1b[2J morning\n\n" + "x" * 200
    excerpt = workflow_offers._excerpt(text)
    assert "\x1b" not in excerpt and "\n" not in excerpt
    assert excerpt.startswith("Every[2J morning x") and len(excerpt) == workflow_offers.EXCERPT


def test_routes_need_the_admin_key(tmp_path):
    store, _, item = offered(tmp_path)
    app = server.app
    app.state.store = store
    app.state.admin_key = "test-admin-key"
    client = TestClient(app)
    admin = {"X-Pi-Key": "test-admin-key"}
    assert client.get("/workflow-proposals/offers").status_code == 401
    assert [
        i["id"] for i in client.get("/workflow-proposals/offers", headers=admin).json()["items"]
    ] == [item["id"]]
    one = client.get(f"/workflow-proposals/offers/{item['id']}", headers=admin).json()
    assert one["messageId"] == item["source"]["messageId"]
    assert client.get("/workflow-proposals/offers/x", headers=admin).status_code == 422
    done = client.post(f"/workflow-proposals/offers/{item['id']}/dismiss", headers=admin)
    assert done.json()["state"] == "dismissed"
