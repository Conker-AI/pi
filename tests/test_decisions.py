"""Tests for decisions and open threads."""

import pytest

from pi.context import decisions
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


class TestDecisionsBasic:
    """Test basic decisions and threads functionality."""

    def test_empty_chat(self, store):
        """Empty chat: revision 0, no items, snapshot None, messages []."""
        session = store.create_session()
        result = decisions.load(store, session)
        assert result["revision"] == 0
        assert result["items"] == []

        with store._connect() as db:
            snap = decisions.snapshot(db, session)
            assert snap is None

        msgs = decisions.messages({})
        assert msgs == []

    def test_add_decision_and_thread(self, store):
        """Add a decision citing a message, add a thread; load shows both with states, revision 2."""
        session = store.create_session()
        msg = store.append_message(session, "user", "Hello")

        # Add a decision
        result = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="Use PostgreSQL",
                cites=[msg["id"]],
            ),
        )
        assert result["revision"] == 1
        assert result["item"]["kind"] == "decision"
        assert result["item"]["text"] == "Use PostgreSQL"
        assert result["item"]["state"] == "active"
        assert result["item"]["author"] == "owner"
        assert result["item"]["cites"] == [msg["id"]]
        assert result["item"]["id"].startswith("dec_")

        # Add a thread
        result2 = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=1,
                kind="thread",
                text="Pick a hosting plan",
                author="ai",
            ),
        )
        assert result2["revision"] == 2
        assert result2["item"]["kind"] == "thread"
        assert result2["item"]["text"] == "Pick a hosting plan"
        assert result2["item"]["state"] == "open"
        assert result2["item"]["author"] == "ai"
        assert result2["item"]["id"].startswith("thr_")

        # Load shows both
        loaded = decisions.load(store, session)
        assert loaded["revision"] == 2
        assert len(loaded["items"]) == 2
        assert loaded["items"][0]["kind"] == "decision"
        assert loaded["items"][0]["state"] == "active"
        assert loaded["items"][1]["kind"] == "thread"
        assert loaded["items"][1]["state"] == "open"

    def test_stale_revision(self, store):
        """Stale revision -> 409."""
        session = store.create_session()
        msg = store.append_message(session, "user", "Hello")

        decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="First",
                cites=[msg["id"]],
            ),
        )

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.add(
                store,
                session,
                decisions.Add(
                    expected_revision=0,
                    kind="decision",
                    text="Second",
                ),
            )
        assert exc.value.code == "revision_conflict"
        assert exc.value.status == 409

    def test_cite_from_another_chat(self, store):
        """A cite from another chat -> 422."""
        session1 = store.create_session()
        session2 = store.create_session()
        msg = store.append_message(session1, "user", "Hello")

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.add(
                store,
                session2,
                decisions.Add(
                    expected_revision=0,
                    kind="decision",
                    text="Bad cite",
                    cites=[msg["id"]],
                ),
            )
        assert exc.value.code == "outside_boundary"
        assert exc.value.status == 422

    def test_duplicate_cites(self, store):
        """Duplicate cites -> 422."""
        session = store.create_session()
        msg = store.append_message(session, "user", "Hello")

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.add(
                store,
                session,
                decisions.Add(
                    expected_revision=0,
                    kind="decision",
                    text="Dup cite",
                    cites=[msg["id"], msg["id"]],
                ),
            )
        assert exc.value.code == "outside_boundary"
        assert exc.value.status == 422

    def test_text_too_long(self, store):
        """501 characters -> 422 (Pydantic validates before our code)."""
        from pydantic import ValidationError

        session = store.create_session()

        with pytest.raises((ValidationError, decisions.DecisionError)):
            decisions.add(
                store,
                session,
                decisions.Add(
                    expected_revision=0,
                    kind="decision",
                    text="x" * 501,
                ),
            )

    def test_replaces_on_thread(self, store):
        """replaces on a thread -> 422."""
        session = store.create_session()

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.add(
                store,
                session,
                decisions.Add(
                    expected_revision=0,
                    kind="thread",
                    text="Thread",
                    replaces="dec_abc",
                ),
            )
        assert exc.value.code == "invalid_replace"
        assert exc.value.status == 422


class TestSupersede:
    """Test supersede behaviour."""

    def test_supersede_active_decision(self, store):
        """A decision replacing an active one marks the old one superseded."""
        session = store.create_session()
        msg = store.append_message(session, "user", "Hello")

        result1 = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="Use PostgreSQL",
                cites=[msg["id"]],
            ),
        )
        old_id = result1["item"]["id"]

        result2 = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=1,
                kind="decision",
                text="Use MySQL",
                replaces=old_id,
            ),
        )
        assert result2["item"]["replaces"] == old_id
        assert result2["item"]["state"] == "active"

        # Old decision should be superseded
        loaded = decisions.load(store, session)
        old_item = next(i for i in loaded["items"] if i["id"] == old_id)
        assert old_item["state"] == "superseded"

    def test_replace_superseded_decision(self, store):
        """Replacing a superseded decision -> 409."""
        session = store.create_session()

        result1 = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="First",
            ),
        )
        old_id = result1["item"]["id"]

        decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=1,
                kind="decision",
                text="Second",
                replaces=old_id,
            ),
        )

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.add(
                store,
                session,
                decisions.Add(
                    expected_revision=2,
                    kind="decision",
                    text="Third",
                    replaces=old_id,
                ),
            )
        assert exc.value.code == "not_replaceable"
        assert exc.value.status == 409

    def test_replace_withdrawn_decision(self, store):
        """Replacing a withdrawn decision -> 409."""
        session = store.create_session()

        result1 = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="First",
            ),
        )
        old_id = result1["item"]["id"]

        decisions.change_state(
            store,
            session,
            old_id,
            decisions.State(
                expected_revision=1,
                state="withdrawn",
            ),
        )

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.add(
                store,
                session,
                decisions.Add(
                    expected_revision=2,
                    kind="decision",
                    text="Second",
                    replaces=old_id,
                ),
            )
        assert exc.value.code == "not_replaceable"
        assert exc.value.status == 409

    def test_change_superseded_decision(self, store):
        """Changing a superseded decision -> 409."""
        session = store.create_session()

        result1 = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="First",
            ),
        )
        old_id = result1["item"]["id"]

        decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=1,
                kind="decision",
                text="Second",
                replaces=old_id,
            ),
        )

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.change_state(
                store,
                session,
                old_id,
                decisions.State(
                    expected_revision=2,
                    state="withdrawn",
                ),
            )
        assert exc.value.code == "superseded"
        assert exc.value.status == 409


class TestStateTransitions:
    """Test state transitions for decisions and threads."""

    def test_thread_resolve_and_reopen(self, store):
        """Thread resolve and reopen."""
        session = store.create_session()

        result = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="thread",
                text="Pick hosting",
            ),
        )
        item_id = result["item"]["id"]

        # Resolve
        decisions.change_state(
            store,
            session,
            item_id,
            decisions.State(
                expected_revision=1,
                state="resolved",
            ),
        )
        loaded = decisions.load(store, session)
        item = next(i for i in loaded["items"] if i["id"] == item_id)
        assert item["state"] == "resolved"

        # Reopen
        decisions.change_state(
            store,
            session,
            item_id,
            decisions.State(
                expected_revision=2,
                state="open",
            ),
        )
        loaded = decisions.load(store, session)
        item = next(i for i in loaded["items"] if i["id"] == item_id)
        assert item["state"] == "open"

    def test_decision_withdraw_and_reactivate(self, store):
        """Decision withdraw and reactivate."""
        session = store.create_session()

        result = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="Use PostgreSQL",
            ),
        )
        item_id = result["item"]["id"]

        # Withdraw
        decisions.change_state(
            store,
            session,
            item_id,
            decisions.State(
                expected_revision=1,
                state="withdrawn",
            ),
        )
        loaded = decisions.load(store, session)
        item = next(i for i in loaded["items"] if i["id"] == item_id)
        assert item["state"] == "withdrawn"

        # Reactivate
        decisions.change_state(
            store,
            session,
            item_id,
            decisions.State(
                expected_revision=2,
                state="active",
            ),
        )
        loaded = decisions.load(store, session)
        item = next(i for i in loaded["items"] if i["id"] == item_id)
        assert item["state"] == "active"

    def test_invalid_thread_to_active(self, store):
        """Thread -> active -> 409."""
        session = store.create_session()

        result = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="thread",
                text="Thread",
            ),
        )
        item_id = result["item"]["id"]

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.change_state(
                store,
                session,
                item_id,
                decisions.State(
                    expected_revision=1,
                    state="active",
                ),
            )
        assert exc.value.code == "invalid_state"
        assert exc.value.status == 409

    def test_invalid_decision_to_resolved(self, store):
        """Decision -> resolved -> 409."""
        session = store.create_session()

        result = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="Decision",
            ),
        )
        item_id = result["item"]["id"]

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.change_state(
                store,
                session,
                item_id,
                decisions.State(
                    expected_revision=1,
                    state="resolved",
                ),
            )
        assert exc.value.code == "invalid_state"
        assert exc.value.status == 409


class TestAuthorship:
    """Test authorship rules."""

    def test_ai_cannot_change_owner_item(self, store):
        """by='ai' changing an owner item -> 403."""
        session = store.create_session()

        result = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="decision",
                text="Owner decision",
                author="owner",
            ),
        )
        item_id = result["item"]["id"]

        with pytest.raises(decisions.DecisionError) as exc:
            decisions.change_state(
                store,
                session,
                item_id,
                decisions.State(
                    expected_revision=1,
                    state="withdrawn",
                    by="ai",
                ),
            )
        assert exc.value.code == "owner_item"
        assert exc.value.status == 403

    def test_ai_can_change_ai_item(self, store):
        """Changing an ai item works."""
        session = store.create_session()

        result = decisions.add(
            store,
            session,
            decisions.Add(
                expected_revision=0,
                kind="thread",
                text="AI thread",
                author="ai",
            ),
        )
        item_id = result["item"]["id"]

        decisions.change_state(
            store,
            session,
            item_id,
            decisions.State(
                expected_revision=1,
                state="resolved",
                by="ai",
            ),
        )
        loaded = decisions.load(store, session)
        item = next(i for i in loaded["items"] if i["id"] == item_id)
        assert item["state"] == "resolved"
