"""Next-message suggestions: what Jev is sent, what comes back, and the route's keys."""

import hashlib
import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi.api import server as api
from pi.api.browser_contract import owner_allowed, runtime_allowed
from pi.conversation import settings as session_settings
from pi.conversation import suggestions
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.owner import settings as owner_settings

ADMIN = "synthetic-admin"
OWNER = "suggestions_test_owner_key"


class Recording:
    """Adapter that records calls and returns a fixed response."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.messages = []

    def complete_bounded(self, messages, *, model, timeout):
        from pi.models.providers import Completion

        self.calls.append((model, timeout))
        self.messages.append(messages)
        return Completion(text=self.response, model=model, provider="recording")


def config():
    disabled = dict(
        enabled=False,
        eligibleModelIds=[],
        modelId=None,
        timeoutMs=1000,
        failure="stop",
        fallbackModelId=None,
    )
    return dict(
        providers=[
            dict(id="one", name="Provider", enabled=True),
            dict(id="two", name="Other", enabled=True),
        ],
        models=[
            dict(
                id="a",
                providerId="one",
                name="A",
                route="actual-a",
                enabled=True,
                routingDescription="",
            ),
            dict(
                id="b",
                providerId="two",
                name="B",
                route="actual-b",
                enabled=True,
                routingDescription="",
            ),
        ],
        defaultModelId="a",
        roleSettings=dict(
            answerMode="manual",
            roles={
                **{role: dict(disabled) for role in model_roles.ROLES},
                "answer": {
                    **disabled,
                    "enabled": True,
                    "eligibleModelIds": ["a", "b"],
                    "modelId": "a",
                },
            },
        ),
    )


def configured(store):
    value = config()
    value["roleSettings"]["roles"]["routing"] = {
        **value["roleSettings"]["roles"]["answer"],
        "enabled": True,
    }
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


def store_setting(store, value):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(
        store,
        owner_settings.Update(
            expected_revision=revision, values={"chats.suggestions_count": value}
        ),
    )


def exchange(store, sid=""):
    """A chat with one full exchange; returns the store and the messages it holds."""
    sid = sid or store.create_session()
    asked = store.append_message(sid, "user", "Hello")
    answered = store.append_message(sid, "assistant", "Hi")
    return asked, answered


# --- 1. What is sent and what comes back ---


def test_suggest_uses_last_exchange(tmp_path):
    adapter = Recording(json.dumps({"suggestions": ["Book hotels", "What to pack?"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "Plan a trip")
        store.append_message(sid, "assistant", "Rome or Paris?")
        store.append_message(sid, "user", "Rome")
        last = store.append_message(sid, "assistant", "Rome in May is good.")
        result = suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert result == {
        "messageId": last["id"],
        "suggestions": ["Book hotels", "What to pack?"],
        "modelId": "a",
    }
    sent = json.loads(adapter.messages[0][1].content)
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant"]
    assert sent["messages"][0]["content"] == "Rome"
    assert sent["messages"][1]["content"] == "Rome in May is good."


def test_count_defaults_to_two_and_follows_the_setting(tmp_path):
    reply = json.dumps({"suggestions": ["One", "Two", "Three"]})
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        exchange(store, sid)
        default = Recording(reply)
        result = suggestions.suggest(store, {"one": default}, sid, suggestions.Ask())
        assert len(result["suggestions"]) == 2
        assert "2 different messages" in default.messages[0][0].content
        store_setting(store, 3)
        three = Recording(reply)
        result = suggestions.suggest(store, {"one": three}, sid, suggestions.Ask())
        assert len(result["suggestions"]) == 3
        assert "3 different messages" in three.messages[0][0].content
        one = Recording(reply)
        result = suggestions.suggest(store, {"one": one}, sid, suggestions.Ask(count=1))
        assert len(result["suggestions"]) == 1


@pytest.mark.parametrize("stored", [0, 4, 2.5, True, "3"])
def test_setting_out_of_range_uses_two(tmp_path, stored):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        exchange(store, sid)
        store_setting(store, stored)
        result = suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert len(result["suggestions"]) == 2


def test_ask_count_is_bounded():
    with pytest.raises(ValidationError):
        suggestions.Ask(count=0)
    with pytest.raises(ValidationError):
        suggestions.Ask(count=4)


def test_unusable_items_are_dropped(tmp_path):
    items = ["  Next  ", "next", "x" * 121, "a‮b", "red\x1b[31m", 7, "", "Last"]
    adapter = Recording(json.dumps({"suggestions": items}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        exchange(store, sid)
        result = suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask(count=3))
    assert result["suggestions"] == ["Next", "Last"]


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        '{"suggestions": "x"}',
        '{"suggestions": [], "extra": 1}',
        '{"suggestions": []}',
        '{"suggestions": ["\u0007"]}',
        '["a"]',
    ],
)
def test_malformed_replies_fail_closed(tmp_path, reply):
    adapter = Recording(reply)
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        exchange(store, sid)
        with pytest.raises(suggestions.SuggestionError) as raised:
            suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert raised.value.status == 503 and raised.value.code == "unavailable"


# --- 2. The chat's filters ---


def test_excluded_messages_are_not_sent(tmp_path):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "First question")
        first = store.append_message(sid, "assistant", "First answer")
        store.append_message(sid, "user", "Second question")
        last = store.append_message(sid, "assistant", "Second answer")
        with store._connect() as db:
            policy = {
                "sessionInstructions": "",
                "messagePolicies": {last["id"]: "exclude"},
                "budget": {
                    "contextWindowTokens": 100000,
                    "outputReserveTokens": 1000,
                    "otherInputTokens": 0,
                },
            }
            db.execute("INSERT INTO context_policies VALUES (?,1,?)", (sid, json.dumps(policy)))
            db.commit()
        result = suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert result["messageId"] == first["id"]
    sent = json.loads(adapter.messages[0][1].content)
    assert [m["content"] for m in sent["messages"]] == ["First question", "First answer"]


def test_redacted_and_forgotten_messages_are_not_sent(tmp_path, monkeypatch):
    adapter = Recording(json.dumps({"suggestions": ["One"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        rows = [
            store.append_message(sid, role, text)
            for role, text in (
                ("user", "Kept question"),
                ("user", "Redacted question"),
                ("assistant", "Kept answer"),
                ("assistant", "Forgotten answer"),
            )
        ]
        rows[1] = {**rows[1], "redacted": True}
        rows[3] = {**rows[3], "content_status": "forgotten"}
        monkeypatch.setattr(suggestions.controls, "history", lambda *_: rows)
        result = suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert result["messageId"] == rows[2]["id"]
    sent = json.loads(adapter.messages[0][1].content)
    assert [m["content"] for m in sent["messages"]] == ["Kept question", "Kept answer"]


def test_answer_without_question(tmp_path):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "assistant", "Hi")
        suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    sent = json.loads(adapter.messages[0][1].content)
    assert [m["role"] for m in sent["messages"]] == ["assistant"]


def test_no_answer_409_empty(tmp_path):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        with pytest.raises(suggestions.SuggestionError) as raised:
            suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert raised.value.status == 409 and raised.value.code == "empty"
    assert adapter.calls == []


# --- 3. Refusals ---


def test_helpers_off_409_and_zero_calls(tmp_path):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings={
                    "agentId": "companion",
                    "privacy": {"memoryDisabled": False, "harnessDisabled": True},
                },
            ),
        )
        exchange(store, sid)
        with pytest.raises(suggestions.SuggestionError) as raised:
            suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert raised.value.status == 409 and raised.value.code == "helpers_off"
    assert adapter.calls == []


@pytest.mark.parametrize("saved", [False, True], ids=["no settings", "routing off"])
def test_no_configuration_503(tmp_path, saved):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        if saved:
            update = model_roles.Configuration.model_validate(config())
            model_roles.save(store, model_roles.Update(expected_revision=0, configuration=update))
        sid = store.create_session()
        exchange(store, sid)
        with pytest.raises(suggestions.SuggestionError) as raised:
            suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert raised.value.status == 503 and raised.value.code == "not_configured"
    assert adapter.calls == []


def test_unknown_chat_404(tmp_path):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        pytest.raises(suggestions.SuggestionError) as raised,
    ):
        suggestions.suggest(store, {"one": adapter}, "ses_missing", suggestions.Ask())
    assert raised.value.status == 404 and raised.value.code == "not_found"


def test_nothing_is_saved(tmp_path):
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        exchange(store, sid)

        def counts():
            with store._connect() as db:
                tables = [
                    row["name"]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                ]
                return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

        before = counts()
        suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
        after = counts()
    assert before == after


# --- 4. Routes ---


@pytest.fixture
def http(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(OWNER.encode()).hexdigest(),
            raising=False,
        )
        if not hasattr(api.app.state, "router") or api.app.state.router is None:
            from unittest.mock import MagicMock

            router_mock = MagicMock()
            router_mock.adapters.return_value = {}
            monkeypatch.setattr(api.app.state, "router", router_mock, raising=False)
        yield store, TestClient(api.app)


def test_route_accepts_owner_key(http):
    store, client = http
    configured(store)
    sid = store.create_session()
    response = client.post(
        f"/sessions/{sid}/suggestions", headers={"X-Pi-Owner-Key": OWNER}, json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "empty"


def test_route_accepts_admin_key(http):
    store, client = http
    configured(store)
    sid = store.create_session()
    response = client.post(f"/sessions/{sid}/suggestions", headers={"X-Pi-Key": ADMIN}, json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "empty"


def test_route_refuses_no_key(http):
    store, client = http
    sid = store.create_session()
    assert client.post(f"/sessions/{sid}/suggestions", json={}).status_code == 401


def test_route_refuses_extra_fields(http):
    store, client = http
    sid = store.create_session()
    response = client.post(
        f"/sessions/{sid}/suggestions",
        headers={"X-Pi-Owner-Key": OWNER},
        json={"count": 2, "other": 1},
    )
    assert response.status_code == 422


def test_the_browser_may_ask_only_by_post_to_one_chat():
    path = "/sessions/ses_0123456789abcdef/suggestions"
    assert owner_allowed("POST", path)
    assert not owner_allowed("GET", path)
    # The conversation credential never gets it, and the pattern names one chat, nothing else.
    assert not runtime_allowed("POST", path)
    for other in ("/sessions/a/b/suggestions", "/sessions//suggestions", path + "/x"):
        assert not owner_allowed("POST", other)
