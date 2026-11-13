"""The Inbox routes for questions, change requests and handled actions describe their replies in
OpenAPI (D-01), and what they send matches the description."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_change_requests import schedule

from pi.agents import asks, change_requests
from pi.agents.inbox_replies import AskList, AskView, ChangeRequestList
from pi.api import server as api
from pi.conversation.store import Store
from pi.turns import access

ADMIN = "reply-models-inbox-admin"
VIEW = change_requests.ChangeRequestView
ROUTES = {
    ("get", "/asks"): AskList,
    ("get", "/asks/{identity}"): AskView,
    ("post", "/asks/{identity}/answer"): AskView,
    ("post", "/asks/{identity}/dismiss"): AskView,
    ("get", "/change-requests"): ChangeRequestList,
    ("get", "/change-requests/{identity}"): VIEW,
    **{
        ("post", f"/change-requests/{{identity}}/{verb}"): VIEW
        for verb in ("approve", "saved", "retry", "published", "decline")
    },
    ("get", "/access/handled"): access.HandledList,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            api.app.state, "owner_key_hash", hashlib.sha256(b"unused").hexdigest(), raising=False
        )
        monkeypatch.setattr(api.app.state, "toolgate", None, raising=False)
        yield store, TestClient(api.app, headers={"X-Pi-Key": ADMIN})


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_each_route_names_its_reply_model_in_openapi(method, path):
    reply = api.app.openapi()["paths"][path][method]["responses"]["200"]
    schema = reply["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{ROUTES[(method, path)].__name__}"}


def test_the_replies_match_their_models(client):
    store, http = client
    chat = store.create_session(title="Plans")
    turn = store.start_turn(chat)
    first = asks.file(store, chat, turn, "Which day?", ["Monday", "Friday"])
    second = asks.file(store, chat, turn, "Any time?", None)
    access.record(
        store,
        session_id=chat,
        turn_id=turn,
        action_id="act-1",
        tool_id="tool",
        decided_by="ai",
        reason="fine",
    )
    schedule(store)
    schedule(store, name="Daily note", repeat="daily")

    AskList.model_validate_json(http.get("/asks").text)
    AskView.model_validate_json(http.get(f"/asks/{first['id']}").text)
    AskView.model_validate_json(http.post(f"/asks/{second['id']}/dismiss").text)
    # The answer route returns this view after passing his words on to the chat.
    AskView.model_validate(asks.answer(store, first["id"], "Monday", chosen=True))
    assert len(AskList.model_validate_json(http.get("/asks").text).results) == 0

    listed = ChangeRequestList.model_validate_json(http.get("/change-requests").text).results
    VIEW.model_validate_json(http.get(f"/change-requests/{listed[0].id}").text)
    VIEW.model_validate_json(http.post(f"/change-requests/{listed[0].id}/decline").text)
    VIEW.model_validate_json(http.post(f"/change-requests/{listed[1].id}/approve").text)

    handled = access.HandledList.model_validate_json(http.get("/access/handled").text)
    assert [item.toolId for item in handled.results] == ["tool"]
