"""Asked in chat for something recurring, Pi offers a workflow draft in the Inbox; nothing more."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import inbox, workflow_offers, workflow_proposals
from pi.api import server
from pi.conversation import forgetting
from pi.conversation.store import Store
from pi.models.routing import Router
from pi.setup import choices as setup_choices
from pi.turns.loop import Loop
from tests.test_tool_turns import FakeGate, Scripted

WEATHER = "Every morning tell me the weather in Tel Aviv"


@pytest.mark.parametrize(
    "text",
    [
        WEATHER,
        "Remind me every Monday to water the plants",
        "please send me a summary of my inbox each evening",
        "Can you check the server every 2 hours and let me know if it is down",
        "Make me a workflow that backs up my notes",
        "Каждое утро присылай мне погоду",
        "Напоминай мне ежедневно пить воду",
        "Создай мне автоматизацию для резервных копий",
    ],
)
def test_requests_for_something_recurring_are_offered(text):
    assert workflow_offers.asks_for_workflow(text)


@pytest.mark.parametrize(
    "text",
    [
        "I go to the gym every morning",
        "Why do I wake up every night?",
        "Why does my phone send me alerts every morning?",
        "What is a workflow?",
        "Tell me the weather in Tel Aviv",
        "Every morning I feel tired",
        "Как погода каждое утро?",
        "",
    ],
)
def test_everything_else_is_left_alone(text):
    assert not workflow_offers.asks_for_workflow(text)


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


def test_the_companion_mentions_the_offer_and_the_inbox_holds_it(tmp_path):
    store, loop, provider = build(tmp_path, ["I put an offer in your Inbox.", "Sunny."])
    session = store.create_session()
    loop.run_turn(session, WEATHER)
    note = [m.content for m in provider.sent[0] if m.role == "system" and "Inbox" in m.content]
    assert len(note) == 1 and "Nothing is drafted" in note[0]
    [item] = offers_in(store)
    assert item["title"] == "Draft a workflow from your message? " + WEATHER
    assert item["source"]["sessionId"] == session

    # An ordinary message neither adds an offer nor repeats the note.
    loop.run_turn(session, "Thanks, what is it like now?")
    assert len(offers_in(store)) == 1
    assert not any("Inbox" in m.content for m in provider.sent[1] if m.role == "system")


def test_no_offer_when_the_owner_has_no_tools(tmp_path):
    store, loop, provider = build(tmp_path, ["Sure.", "Sure."])
    loop.toolgate = None
    loop.run_turn(store.create_session(), WEATHER)
    loop.toolgate = FakeGate()
    setup_choices.record(
        store,
        "capabilities",
        setup_choices.ChoiceInput(requestId="skip-tools", choice="skip", expectedRevision=0),
    )
    loop.run_turn(store.create_session(), WEATHER)
    assert offers_in(store) == []
    assert not any(
        "Inbox" in m.content for sent in provider.sent for m in sent if m.role == "system"
    )


def test_the_companions_own_grants_do_not_matter(tmp_path):
    # ToolGate grants the companion nothing, but the owner's tools can still run a workflow.
    store, loop, _ = build(tmp_path, ["Sure."], FakeGate(unavailable=True))
    loop.run_turn(store.create_session(), WEATHER)
    assert len(offers_in(store)) == 1


def offered(tmp_path):
    store, loop, _ = build(tmp_path, ["Noted."])
    session = store.create_session()
    loop.run_turn(session, WEATHER)
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


@pytest.mark.parametrize(
    "execution",
    [
        {"kind": "agent", "privacy": {"harnessDisabled": False}},
        {"kind": "companion", "privacy": {"harnessDisabled": True}},
    ],
)
def test_only_the_companions_own_chats_with_the_harness_on(tmp_path, execution):
    store, loop, _ = build(tmp_path, ["Hello."])
    loop.run_turn(store.create_session(), "Hello")
    with store._connect() as db:
        turn = db.execute("SELECT id FROM turns").fetchone()["id"]
    assert workflow_offers.consider(store, execution, turn, WEATHER, tools=True) is None
    companion = {"kind": "companion", "privacy": {"harnessDisabled": False}}
    assert workflow_offers.consider(store, companion, turn, WEATHER, tools=True)["state"] == "open"
