"""Chat names: owner rename and Jev's automatic name."""

import json
from contextlib import closing

import pytest

from pi.conversation import naming
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models import roles as model_roles


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


class InterceptingRecording:
    """Adapter that calls a side-effect during complete_bounded."""

    def __init__(self, response, side_effect):
        self.response = response
        self.side_effect = side_effect
        self.calls = []

    def complete_bounded(self, messages, *, model, timeout):
        from pi.models.providers import Completion

        self.calls.append((model, timeout))
        self.side_effect()
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


def configured(store, routing_enabled=True):
    value = config()
    value["roleSettings"]["roles"]["routing"] = {
        **value["roleSettings"]["roles"]["answer"],
        "enabled": routing_enabled,
    }
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


# --- 1. Unsaved ---


@pytest.mark.parametrize("title,expected", [("", ""), ("My chat", "My chat")])
def test_unsaved_session_has_revision_0_and_source_none(tmp_path, title, expected):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session(title=title)
        result = naming.load(store, sid)
    assert result == {"revision": 0, "title": expected, "source": None, "modelId": None}


# --- 2. Rename ---


def test_rename_stores_title_source_owner_revision_1(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        result = naming.rename(store, sid, naming.Rename(expected_revision=0, title="New name"))
    assert result["revision"] == 1
    assert result["title"] == "New name"
    assert result["source"] == "owner"
    assert result["modelId"] is None


def test_rename_stale_revision_conflict(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        naming.rename(store, sid, naming.Rename(expected_revision=0, title="First"))
        with pytest.raises(naming.NamingError) as raised:
            naming.rename(store, sid, naming.Rename(expected_revision=0, title="Second"))
    assert raised.value.status == 409 and raised.value.code == "revision_conflict"


@pytest.mark.parametrize("bad_title", ["  ", "a" * 61, "bad\ntitle"])
def test_rename_refuses_invalid_titles(tmp_path, bad_title):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        with pytest.raises(ValueError):
            naming.rename(store, sid, naming.Rename(expected_revision=0, title=bad_title))


def test_rename_unknown_chat_404(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store, pytest.raises(naming.NamingError) as raised:
        naming.rename(store, "ses_missing", naming.Rename(expected_revision=0, title="X"))
    assert raised.value.status == 404


# --- 3. Auto name with messages ---


def test_auto_name_sends_both_messages_and_saves(tmp_path):
    adapter = Recording('{"title": "Laptop comparison"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "What is the best laptop?")
        store.append_message(sid, "assistant", "The ThinkPad is great.")
        result = naming.auto_name(
            store, {"one": adapter}, sid, naming.AutoName(expected_revision=0)
        )
    assert result["title"] == "Laptop comparison"
    assert result["source"] == "auto"
    assert result["modelId"] == "a"
    assert result["revision"] == 1
    assert len(adapter.calls) == 1
    # The prompt contains the user and assistant messages
    sent_messages = adapter.messages[0]
    user_content = json.loads(sent_messages[1].content)
    assert len(user_content["messages"]) == 2
    assert user_content["messages"][0]["role"] == "user"
    assert user_content["messages"][1]["role"] == "assistant"
    # Verify the title was saved correctly
    with closing(Store(tmp_path / "pi.db")) as store:
        loaded = naming.load(store, sid)
    assert loaded["title"] == "Laptop comparison"
    assert loaded["source"] == "auto"


def test_auto_name_cuts_messages_to_source_limit(tmp_path):
    long_text = "x" * 3000
    adapter = Recording('{"title": "Long chat"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", long_text)
        store.append_message(sid, "assistant", long_text)
        result = naming.auto_name(
            store, {"one": adapter}, sid, naming.AutoName(expected_revision=0)
        )
    assert result["title"] == "Long chat"


def test_auto_name_excludes_excluded_messages(tmp_path):
    adapter = Recording('{"title": "Included only"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        msg1 = store.append_message(sid, "user", "Include this")  # noqa: F841
        msg2 = store.append_message(sid, "assistant", "Also include")
        # Set a context policy that excludes msg2
        with store._connect() as db:
            policy = {
                "sessionInstructions": "",
                "messagePolicies": {msg2["id"]: "exclude"},
                "budget": {
                    "contextWindowTokens": 100000,
                    "outputReserveTokens": 1000,
                    "otherInputTokens": 0,
                },
            }
            db.execute(
                "INSERT INTO context_policies VALUES (?,1,?)",
                (sid, json.dumps(policy)),
            )
            db.commit()
        result = naming.auto_name(
            store, {"one": adapter}, sid, naming.AutoName(expected_revision=0)
        )
    assert result["title"] == "Included only"
    # The prompt should not contain the excluded message text
    sent_messages = adapter.messages[0]
    user_content = json.loads(sent_messages[1].content)
    assert len(user_content["messages"]) == 1
    assert user_content["messages"][0]["role"] == "user"
    assert "Include this" in user_content["messages"][0]["content"]
    assert "Also include" not in json.dumps(user_content)


# --- 4. Malformed helper replies ---


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        '{"title": "OK", "extra": 1}',
        '{"title": ""}',
        '{"title": "' + "a" * 61 + '"}',
        '{"title": "bad\\ntitle"}',
    ],
)
def test_malformed_helper_replies_fail_closed(tmp_path, reply):
    adapter = Recording(reply)
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        with pytest.raises(naming.NamingError) as raised:
            naming.auto_name(store, {"one": adapter}, sid, naming.AutoName(expected_revision=0))
    assert raised.value.status == 503 and raised.value.code == "unavailable"
    # Nothing was saved
    with closing(Store(tmp_path / "pi.db")) as store:
        loaded = naming.load(store, sid)
    assert loaded["revision"] == 0 and loaded["source"] is None


# --- 5. Helpers off, no configuration, no messages ---


def test_helpers_off_409_and_zero_calls(tmp_path):
    adapter = Recording('{"title": "Name"}')
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
        with pytest.raises(naming.NamingError) as raised:
            naming.auto_name(store, {"one": adapter}, sid, naming.AutoName(expected_revision=0))
    assert raised.value.status == 409 and raised.value.code == "helpers_off"
    assert adapter.calls == []


def test_no_configuration_503(tmp_path):
    adapter = Recording('{"title": "Name"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        with pytest.raises(naming.NamingError) as raised:
            naming.auto_name(store, {"one": adapter}, sid, naming.AutoName(expected_revision=0))
    assert raised.value.status == 503 and raised.value.code == "not_configured"
    assert adapter.calls == []


def test_routing_off_503_not_configured_and_zero_calls(tmp_path):
    adapter = Recording('{"title": "Name"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store, routing_enabled=False)
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi")
        with pytest.raises(naming.NamingError) as raised:
            naming.auto_name(store, {"one": adapter}, sid, naming.AutoName(expected_revision=0))
    assert raised.value.status == 503 and raised.value.code == "not_configured"
    assert adapter.calls == []


def test_no_messages_409_empty(tmp_path):
    adapter = Recording('{"title": "Name"}')
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        with pytest.raises(naming.NamingError) as raised:
            naming.auto_name(store, {"one": adapter}, sid, naming.AutoName(expected_revision=0))
    assert raised.value.status == 409 and raised.value.code == "empty"
    assert adapter.calls == []


# --- 6. Owner rename wins during auto_name ---


def test_owner_rename_during_auto_name_wins(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        sid = store.create_session()
        store.append_message(sid, "user", "Hello")
        store.append_message(sid, "assistant", "Hi there")

        # The adapter will call rename during complete_bounded, simulating a concurrent owner rename
        def owner_rename_during_call():
            naming.rename(store, sid, naming.Rename(expected_revision=0, title="Owner title"))

        adapter = InterceptingRecording('{"title": "Auto title"}', owner_rename_during_call)
        with pytest.raises(naming.NamingError) as raised:
            naming.auto_name(store, {"one": adapter}, sid, naming.AutoName(expected_revision=0))
        assert raised.value.status == 409 and raised.value.code == "revision_conflict"
        # The owner's title stays
        loaded = naming.load(store, sid)
    assert loaded["title"] == "Owner title"
    assert loaded["source"] == "owner"
