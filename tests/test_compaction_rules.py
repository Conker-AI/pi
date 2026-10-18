"""Compaction rules: refusals write nothing, hidden answer versions, restore chains."""

from contextlib import closing

import pytest
from test_loop import Recorder, loop_with

from pi.context import compaction, controls, summaries
from pi.conversation.store import Store
from tests.test_response_versions import alternative


def with_policy(store, session):
    controls.save(
        store,
        session,
        controls.Update(
            expected_revision=0,
            policy=controls.Policy(
                sessionInstructions="",
                messagePolicies={},
                budget=controls.Budget(
                    contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                ),
            ),
        ),
    )


def fold(store, session, covers, policy_revision=1, summary_revision=0):
    return compaction.compact(
        store,
        session,
        compaction.Compact(
            expected_policy_revision=policy_revision,
            expected_summary_revision=summary_revision,
            summary="Folded summary",
            covers=covers,
        ),
    )


def test_a_hidden_answer_version_cannot_be_folded(tmp_path):
    """A rule on an unselected answer version would make every later turn fail."""
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        original = loop_with(store, Recorder("Original answer")).run_turn(sid, "Question")[
            "message"
        ]
        _, hidden = alternative(store, sid, original["id"])
        with_policy(store, sid)
        with pytest.raises(controls.ContextError) as raised:
            fold(store, sid, [hidden["id"]])
        assert raised.value.detail["code"] == "inactive_version"
        assert compaction.view(store, sid)["covered"] == []
        assert summaries.load(store, sid)["revision"] == 0
        assert controls.load(store, sid)["revision"] == 1


def test_coverage_follows_restore_chains(tmp_path):
    """Restoring the compacted version folds again, also through a restore of a restore."""
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        first = store.append_message(sid, "user", "Old question")
        with_policy(store, sid)
        fold(store, sid, [first["id"]])
        summaries.save(store, sid, summaries.Restore(expected_revision=1, revision=0))
        assert compaction.view(store, sid)["folded"] == []
        summaries.save(store, sid, summaries.Restore(expected_revision=2, revision=1))
        assert compaction.view(store, sid)["folded"] == [first["id"]]
        summaries.save(store, sid, summaries.Restore(expected_revision=3, revision=3))
        assert compaction.view(store, sid)["folded"] == [first["id"]]


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
