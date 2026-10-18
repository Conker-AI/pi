"""Compaction tests: reviewed compaction folds messages into the summary."""

import json
from contextlib import closing

import pytest

from pi.api import server
from pi.context import compaction, controls, summaries
from pi.conversation.store import Store


def test_compact_covers_two_messages_and_folds_them(tmp_path):
    """Basic compaction: cover two messages, create new revision, fold them."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session(summary="Original summary")
        msg1 = store.append_message(session, "user", "Hello")
        msg2 = store.append_message(session, "assistant", "Hi there!")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Compact covering two messages
        result = compaction.compact(
            store,
            session,
            compaction.Compact(
                expected_policy_revision=1,
                expected_summary_revision=0,
                summary="New summary with folded content",
                covers=[msg1["id"], msg2["id"]],
            ),
        )

        assert result["policyRevision"] == 2
        assert result["summaryRevision"] == 1
        assert set(result["covered"]) == {msg1["id"], msg2["id"]}
        assert set(result["folded"]) == {msg1["id"], msg2["id"]}

        # Check view
        view_result = compaction.view(store, session)
        assert view_result["summaryRevision"] == 1
        assert set(view_result["covered"]) == {msg1["id"], msg2["id"]}
        assert set(view_result["folded"]) == {msg1["id"], msg2["id"]}


def test_select_history_excludes_folded_messages(tmp_path):
    """select_history should not contain folded messages."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")
        msg2 = store.append_message(session, "assistant", "Assistant message")
        msg3 = store.append_message(session, "user", "Another user message")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Compact covering first two messages
        compaction.compact(
            store,
            session,
            compaction.Compact(
                expected_policy_revision=1,
                expected_summary_revision=0,
                summary="Summary",
                covers=[msg1["id"], msg2["id"]],
            ),
        )

        # Get history and policy
        history_rows = controls.history(store, session)
        policy = controls.load(store, session)["policy"]

        # select_history should exclude folded messages
        selected = controls.select_history(policy, history_rows, None)
        selected_ids = {row["id"] for row in selected}

        assert msg1["id"] not in selected_ids  # folded
        assert msg2["id"] not in selected_ids  # folded
        assert msg3["id"] in selected_ids  # not folded


def test_pinning_folded_message_brings_it_back(tmp_path):
    """Pinning a folded message should bring it back to select_history."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")
        store.append_message(session, "assistant", "Assistant message")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Compact covering first message
        compaction.compact(
            store,
            session,
            compaction.Compact(
                expected_policy_revision=1,
                expected_summary_revision=0,
                summary="Summary",
                covers=[msg1["id"]],
            ),
        )

        # Pin the folded message
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=2,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={msg1["id"]: "keep-exact"},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Get history and policy
        history_rows = controls.history(store, session)
        policy = controls.load(store, session)["policy"]

        # select_history should include the pinned message
        selected = controls.select_history(policy, history_rows, None)
        selected_ids = {row["id"] for row in selected}

        assert msg1["id"] in selected_ids  # now pinned, should be included


def test_restoring_summary_unfolds_messages(tmp_path):
    """Restoring a summary version should un-fold messages."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")
        store.append_message(session, "assistant", "Assistant message")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Compact covering first message
        compaction.compact(
            store,
            session,
            compaction.Compact(
                expected_policy_revision=1,
                expected_summary_revision=0,
                summary="New summary",
                covers=[msg1["id"]],
            ),
        )

        # Restore to revision 0 (before compaction)
        summaries.save(
            store,
            session,
            summaries.Restore(expected_revision=1, revision=0),
        )

        # Check view - should show no coverage
        view_result = compaction.view(store, session)
        assert view_result["summaryRevision"] == 2  # new restore revision
        assert len(view_result["covered"]) == 0
        assert len(view_result["folded"]) == 0


def test_compaction_refusals_write_nothing(tmp_path):
    """All refusal cases should write nothing to the database."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")
        store.append_message(session, "assistant", "Assistant message")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Test no policy (by deleting it)
        with store._connect() as db:
            db.execute("DELETE FROM context_policies WHERE session_id=?", (session,))

        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=1,
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[msg1["id"]],
                ),
            )
        assert exc.value.detail["code"] == "no_settings"

        # Policy was deleted, so load should return no policy
        assert controls.load(store, session)["revision"] == 0
        assert controls.load(store, session)["policy"] is None

        # Restore policy for other tests
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Test stale policy revision
        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=999,  # wrong
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[msg1["id"]],
                ),
            )
        assert exc.value.detail["code"] == "revision_conflict"

        # Test stale summary revision
        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=1,
                    expected_summary_revision=999,  # wrong
                    summary="Summary",
                    covers=[msg1["id"]],
                ),
            )
        assert exc.value.detail["code"] == "revision_conflict"

        # Test pinned message
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=1,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={msg1["id"]: "keep-exact"},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=2,
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[msg1["id"]],
                ),
            )
        assert exc.value.detail["code"] == "pinned"

        # Test retrieve message
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=2,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={msg1["id"]: "retrieve"},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=3,
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[msg1["id"]],
                ),
            )
        assert exc.value.detail["code"] == "retrieve_rule"

        # Test message from another chat
        other_session = store.create_session()
        other_msg = store.append_message(other_session, "user", "Other message")

        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=3,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=4,
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[other_msg["id"]],
                ),
            )
        assert exc.value.detail["code"] == "outside_boundary"

        # Test duplicate ID
        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=4,
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[msg1["id"], msg1["id"]],  # duplicate
                ),
            )
        assert exc.value.detail["code"] == "duplicate"


def test_closed_chat_refuses_compaction(tmp_path):
    """Closed chat should refuse compaction."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Close the session
        store.close_session(session, "closed")

        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=1,
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[msg1["id"]],
                ),
            )
        assert exc.value.detail["code"] == "session_closed"


def test_busy_chat_refuses_compaction(tmp_path):
    """Busy chat should refuse compaction."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Create a running turn
        with store._connect() as db:
            db.execute(
                "INSERT INTO turn_submissions(request_id,requested_session_id,state,history_seq,created_at,updated_at) VALUES('busy_request_001',?,'preparing',0,?,?)",
                (session, 0, 0),
            )

        with pytest.raises(controls.ContextError) as exc:
            compaction.compact(
                store,
                session,
                compaction.Compact(
                    expected_policy_revision=1,
                    expected_summary_revision=0,
                    summary="Summary",
                    covers=[msg1["id"]],
                ),
            )
        assert exc.value.detail["code"] == "session_busy"


def test_excluded_message_stays_excluded(tmp_path):
    """Excluded message in covers should stay excluded."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")

        # Create policy with excluded message
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={msg1["id"]: "exclude"},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Compact covering the excluded message
        result = compaction.compact(
            store,
            session,
            compaction.Compact(
                expected_policy_revision=1,
                expected_summary_revision=0,
                summary="Summary",
                covers=[msg1["id"]],
            ),
        )

        # Message should be covered but not folded (stays excluded)
        assert msg1["id"] in result["covered"]
        assert msg1["id"] not in result["folded"]

        # Check policy
        policy = controls.load(store, session)["policy"]
        assert policy["messagePolicies"][msg1["id"]] == "exclude"


def test_summary_draft_skips_folded_messages(tmp_path):
    """Summary draft should skip folded messages."""
    from pi.context import summary_draft
    from tests.test_model_roles import Adapter, config

    class Recording(Adapter):
        def __init__(self, response="Test summary"):
            super().__init__(response)
            self.recorded_messages = []

        def complete_bounded(self, messages, *, model, timeout):
            self.recorded_messages = messages
            return super().complete_bounded(messages, model=model, timeout=timeout)

    adapter = Recording("Test summary")
    with closing(Store(tmp_path / "pi.db")) as store:
        # Configure models
        value = config()
        value["roleSettings"]["roles"]["summarization"] = {
            **value["roleSettings"]["roles"]["answer"],
            "enabled": True,
        }
        from pi.models import roles as model_roles

        model_roles.save(
            store,
            model_roles.Update(
                expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
            ),
        )

        session = store.create_session()
        msg1 = store.append_message(session, "user", "User message")
        msg2 = store.append_message(session, "assistant", "Assistant message")
        msg3 = store.append_message(session, "user", "Another user message")

        # Create policy
        controls.save(
            store,
            session,
            controls.Update(
                expected_revision=0,
                policy=controls.Policy(
                    sessionInstructions="Test",
                    messagePolicies={},
                    budget=controls.Budget(
                        contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )

        # Compact covering first two messages
        compaction.compact(
            store,
            session,
            compaction.Compact(
                expected_policy_revision=1,
                expected_summary_revision=0,
                summary="Summary",
                covers=[msg1["id"], msg2["id"]],
            ),
        )

        # Generate draft
        summary_draft.draft(store, {"one": adapter}, session, summary_draft.Request())

        # Should only include the non-folded message
        user_message = adapter.recorded_messages[1].content  # User message after system
        payload = json.loads(user_message)
        message_ids = [msg["id"] for msg in payload["messages"]]

        assert msg1["id"] not in message_ids  # folded
        assert msg2["id"] not in message_ids  # folded
        assert msg3["id"] in message_ids  # not folded


def test_http_routes_require_admin(tmp_path):
    """HTTP routes should require admin key."""
    from fastapi.testclient import TestClient

    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()

        # Set up app with test store and admin key
        app = server.app
        app.state.store = store
        app.state.admin_key = "test-admin-key"

        client = TestClient(app)

        # Test GET with admin key - should work
        response = client.get(
            f"/context/{session}/compaction",
            headers={"X-Pi-Key": "test-admin-key"},
        )
        assert response.status_code == 200

        # Test GET with owner key - should be refused
        response = client.get(
            f"/context/{session}/compaction",
            headers={"X-Pi-Owner-Key": "test-owner-key"},
        )
        assert response.status_code == 401

        # Test GET with no key - should be refused
        response = client.get(f"/context/{session}/compaction")
        assert response.status_code == 401

        # Test POST with admin key - should work (if we had valid data)
        response = client.post(
            f"/context/{session}/compact",
            json={
                "expected_policy_revision": 0,
                "expected_summary_revision": 0,
                "summary": "Test",
                "covers": [],
            },
            headers={"X-Pi-Key": "test-admin-key"},
        )
        # Will fail validation but should pass auth
        assert response.status_code in (422, 409)  # validation or business logic error, not auth

        # Test POST with owner key - should be refused
        response = client.post(
            f"/context/{session}/compact",
            json={
                "expected_policy_revision": 0,
                "expected_summary_revision": 0,
                "summary": "Test",
                "covers": [],
            },
            headers={"X-Pi-Owner-Key": "test-owner-key"},
        )
        assert response.status_code == 401

        # Test POST with no key - should be refused
        response = client.post(
            f"/context/{session}/compact",
            json={
                "expected_policy_revision": 0,
                "expected_summary_revision": 0,
                "summary": "Test",
                "covers": [],
            },
        )
        assert response.status_code == 401
