from contextlib import closing

import pytest

from pi import context_controls, forgetting, submissions
from pi import context_summaries as summaries
from pi.store import Store


def test_edit_restore_preserves_parent_and_messages(tmp_path):
    with closing(Store(tmp_path / "summary.db")) as store:
        parent = store.create_session()
        message = store.append_message(parent, "user", "Original text")
        child = store.create_session(parent_id=parent, summary="Original summary")
        value = summaries.save(
            store, child, summaries.Edit(expected_revision=0, summary="Corrected")
        )
        assert value["revision"] == 1
        assert value["source_session_id"] == parent
        restored = summaries.save(store, child, summaries.Restore(expected_revision=1, revision=0))
        assert restored["summary"] == "Original summary"
        assert len(restored["versions"]) == 3
        assert store.get_message(message["id"])["content"] == "Original text"
        with pytest.raises(context_controls.ContextError, match="changed"):
            summaries.save(store, child, summaries.Edit(expected_revision=1, summary="Stale"))


def test_pending_submission_blocks_summary_changes(tmp_path):
    with closing(Store(tmp_path / "summary.db")) as store:
        sid = store.create_session(summary="Current")
        submissions.reserve(store, "summary_pending_request", sid, "Question", {})
        with pytest.raises(context_controls.ContextError, match="Finish current work"):
            summaries.save(store, sid, summaries.Edit(expected_revision=0, summary="Changed"))
        assert summaries.load(store, sid)["summary"] == "Current"


def test_automatic_fork_summary_is_versioned_after_owner_edit(tmp_path):
    with closing(Store(tmp_path / "summary.db")) as store:
        sid = store.create_session(summary="Initial")
        summaries.save(store, sid, summaries.Edit(expected_revision=0, summary="Reviewed"))
        store.close_session(sid, "forked", summary="Automatically summarized")
        value = summaries.load(store, sid)
        assert value["revision"] == 2
        assert value["source_session_id"] == sid
        assert [v["summary"] for v in value["versions"]] == [
            "Automatically summarized",
            "Reviewed",
            "Initial",
        ]
        with pytest.raises(context_controls.ContextError, match="changed"):
            summaries.save(store, sid, summaries.Edit(expected_revision=1, summary="Stale edit"))


def test_forgotten_parent_scrubs_child_summary_history(tmp_path):
    path = tmp_path / "summary.db"
    secret = "private-summary-91fc7"
    with closing(Store(path)) as store:
        parent = store.create_session()
        child = store.create_session(parent_id=parent, summary=secret)
        summaries.save(
            store, child, summaries.Edit(expected_revision=0, summary=secret + " edited")
        )
    forgetting.forget(path, parent, forgetting.preview(path, parent)["confirmation"])
    with closing(Store(path)) as store, pytest.raises(context_controls.ContextError):
        summaries.load(store, child)
    for file in tmp_path.iterdir():
        if file.is_file():
            assert secret.encode() not in file.read_bytes()
