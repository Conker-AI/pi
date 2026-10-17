"""Summary drafts: the summarization helper proposes a chat summary for review."""

import json
from contextlib import closing

import pytest

from pi.api import server
from pi.context import controls, summary_draft
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models import roles as model_roles
from tests.test_model_roles import Adapter, config


def configured(store, summarization_enabled=True):
    value = config()
    value["roleSettings"]["roles"]["summarization"] = {
        **value["roleSettings"]["roles"]["answer"],
        "enabled": summarization_enabled,
    }
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


class Recording(Adapter):
    def __init__(self, response="Test summary", fail=False):
        super().__init__(response, fail=fail)
        self.recorded_messages = []

    def complete_bounded(self, messages, *, model, timeout):
        self.recorded_messages = messages
        return super().complete_bounded(messages, model=model, timeout=timeout)


def test_draft_returns_helper_text_with_metadata(tmp_path):
    adapter = Recording("Brief conversation summary")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()
        msg1 = store.append_message(session, "user", "Hello")
        msg2 = store.append_message(session, "assistant", "Hi there!")

        result = summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    assert result["summary"] == "Brief conversation summary"
    assert result["messageIds"] == [msg1["id"], msg2["id"]]
    assert result["truncated"] is False
    assert result["summaryRevision"] == 0
    assert result["modelId"] == "a"
    assert result["configurationRevision"] == 1
    assert len(adapter.calls) == 1


def test_excluded_and_pinned_messages_not_sent(tmp_path):
    adapter = Recording("Test summary")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()
        store.append_message(session, "user", "User content")
        excluded_msg = store.append_message(session, "assistant", "Excluded secret")
        pinned_msg = store.append_message(session, "user", "Pinned important")
        allow_summary_msg = store.append_message(session, "assistant", "Allow summary")
        retrieve_msg = store.append_message(session, "user", "Retrieve content")

        # Set up policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={
                        excluded_msg["id"]: "exclude",
                        pinned_msg["id"]: "keep-exact",
                        allow_summary_msg["id"]: "allow-summary",
                        retrieve_msg["id"]: "retrieve",
                    },
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    user_message = adapter.recorded_messages[1].content  # User message after system
    payload = json.loads(user_message)
    message_ids = [msg["id"] for msg in payload["messages"]]

    assert excluded_msg["id"] not in message_ids
    assert pinned_msg["id"] not in message_ids
    assert allow_summary_msg["id"] in message_ids
    assert retrieve_msg["id"] not in message_ids  # retrieve messages are excluded unless retrieved
    assert "User content" in user_message
    assert "Excluded secret" not in user_message
    assert "Pinned important" not in user_message


def test_existing_summary_sent_as_earlier_summary(tmp_path):
    adapter = Recording("Updated summary")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session(summary="Earlier summary")
        store.append_message(session, "user", "New message")

        result = summary_draft.draft(
            store, {"one": adapter}, session, summary_draft.Request(focus="focus text")
        )

    assert result["earlierSummaryIncluded"] is True
    assert result["summaryRevision"] == 0  # No versions saved yet

    user_message = adapter.recorded_messages[1].content
    payload = json.loads(user_message)
    assert payload["earlierSummary"] == "Earlier summary"
    assert payload["focus"] == "focus text"


def test_long_chat_cut_from_oldest_side(tmp_path):
    adapter = Recording("Truncated summary")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()

        # Create many messages to exceed limit
        messages = []
        for i in range(100):
            msg = store.append_message(
                session, "user" if i % 2 == 0 else "assistant", "Message " + str(i) * 1000
            )
            messages.append(msg)

        result = summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    assert result["truncated"] is True
    # Should include newest messages
    assert result["messageIds"][-1] == messages[-1]["id"]
    # Should not include oldest messages
    assert messages[0]["id"] not in result["messageIds"]


def test_helpers_off_returns_409_no_model_call(tmp_path):
    adapter = Recording("Should not be called")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()

        # Disable helpers
        session_settings.save(
            store,
            session,
            session_settings.Update(
                expected_revision=0,
                settings={
                    "agentId": "companion",
                    "privacy": {"memoryDisabled": False, "harnessDisabled": True},
                },
            ),
        )

        with pytest.raises(summary_draft.DraftError) as exc:
            summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    assert exc.value.status == 409
    assert exc.value.code == "helpers_off"
    assert len(adapter.calls) == 0


def test_no_model_configuration_returns_503_no_call(tmp_path):
    adapter = Recording("Should not be called")
    with closing(Store(tmp_path / "pi.db")) as store:
        # Don't configure models
        session = store.create_session()

        with pytest.raises(summary_draft.DraftError) as exc:
            summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    assert exc.value.status == 503
    assert exc.value.code == "not_configured"
    assert len(adapter.calls) == 0


def test_unknown_chat_returns_404(tmp_path):
    adapter = Recording("Should not be called")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)

        with pytest.raises(summary_draft.DraftError) as exc:
            summary_draft.draft(store, {"one": adapter}, "unknown_session", summary_draft.Request())

    assert exc.value.status == 404
    assert exc.value.code == "not_found"
    assert len(adapter.calls) == 0


@pytest.mark.parametrize(
    "response",
    [
        "",  # Empty reply
        "   ",  # Whitespace reply
        "x" * 16001,  # Too long reply
    ],
)
def test_invalid_responses_return_503(tmp_path, response):
    adapter = Recording(response)
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()
        store.append_message(session, "user", "Hello")

        with pytest.raises(summary_draft.DraftError) as exc:
            summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    assert exc.value.status == 503
    assert exc.value.code == "unavailable"


def test_failing_adapter_returns_503(tmp_path):
    adapter = Recording("Should not be called", fail=True)
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()
        store.append_message(session, "user", "Hello")

        with pytest.raises(summary_draft.DraftError) as exc:
            summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    assert exc.value.status == 503
    assert exc.value.code == "unavailable"


def test_no_user_assistant_messages_returns_409(tmp_path):
    adapter = Recording("Should not be called")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()
        store.append_message(session, "system", "System message")
        store.append_message(session, "tool", "Tool response")

        with pytest.raises(summary_draft.DraftError) as exc:
            summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

    assert exc.value.status == 409
    assert exc.value.code == "empty"


def test_draft_saves_nothing(tmp_path):
    adapter = Recording("Draft summary")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session(summary="Original summary")
        store.append_message(session, "user", "Hello")

        # Load initial state
        initial = summary_draft.summaries.load(store, session)
        initial_revision = initial["revision"]
        initial_summary = initial["summary"]

        # Generate draft
        summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

        # Check nothing changed
        final = summary_draft.summaries.load(store, session)
        assert final["revision"] == initial_revision
        assert final["summary"] == initial_summary


def test_http_route_requires_admin_and_works(tmp_path):
    from fastapi.testclient import TestClient

    adapter = Recording("HTTP draft summary")
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        session = store.create_session()
        store.append_message(session, "user", "Hello")

        # Set up app with test store and admin key
        app = server.app
        app.state.store = store
        app.state.admin_key = "test-admin-key"
        app.state.router = type("obj", (object,), {"adapters": lambda self: {"one": adapter}})()

        client = TestClient(app)

        # Test with admin key - should work
        response = client.post(
            f"/context/{session}/summary/draft",
            json={"focus": "test focus"},
            headers={"X-Pi-Key": "test-admin-key"},
        )
        assert response.status_code == 200
        assert response.json()["summary"] == "HTTP draft summary"

        # Test with owner key - should be refused
        response = client.post(
            f"/context/{session}/summary/draft",
            json={"focus": "test focus"},
            headers={"X-Pi-Owner-Key": "test-owner-key"},
        )
        assert response.status_code == 401

        # Test with no key - should be refused
        response = client.post(f"/context/{session}/summary/draft", json={"focus": "test focus"})
        assert response.status_code == 401
