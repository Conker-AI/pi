from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from pi import drafts, forgetting, submissions, tasks
from pi.store import Store


def test_concurrent_edits_and_clear_reject_stale_writer(tmp_path):
    with closing(Store(tmp_path / "draft.db")) as store:
        sid = store.create_session()

        def write(text):
            try:
                return drafts.save(store, sid, drafts.Save(expected_revision=0, text=text))
            except drafts.DraftError:
                return None

        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(write, ["one", "two"]))
        assert sum(item is not None for item in result) == 1
        assert drafts.save(store, sid, drafts.Save(expected_revision=1, text=""))["revision"] == 2
        with pytest.raises(drafts.DraftError, match="another window"):
            drafts.save(store, sid, drafts.Save(expected_revision=1, text="stale"))
        assert drafts.load(store, sid)["text"] == ""


def test_task_draft_is_separate_and_cannot_cross_sessions(tmp_path):
    with closing(Store(tmp_path / "draft.db")) as store:
        sid, other = store.create_session(), store.create_session()
        task = tasks.create(
            store,
            tasks.CreateTask(
                request_id="draft_task_request",
                session_id=sid,
                outcome="Report",
                criteria=["Written"],
            ),
        )
        drafts.save(store, sid, drafts.Save(expected_revision=0, text="chat"))
        drafts.save(store, sid, drafts.Save(expected_revision=0, text="task"), task["id"])
        assert drafts.load(store, sid)["text"] == "chat"
        assert drafts.load(store, sid, task["id"])["text"] == "task"
        with pytest.raises(drafts.DraftError, match="does not belong"):
            drafts.load(store, other, task["id"])
        assert store.messages(sid) == []


def test_restart_and_offline_forgetting_scrubs_drafts(tmp_path):
    path = tmp_path / "draft.db"
    secret = "private-draft-unique-217829"
    with closing(Store(path)) as store:
        sid = store.create_session()
        drafts.save(store, sid, drafts.Save(expected_revision=0, text=secret))
    with closing(Store(path)) as store:
        assert drafts.load(store, sid)["text"] == secret
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as store, pytest.raises(drafts.DraftError, match="unavailable"):
        drafts.load(store, sid)
    for file in tmp_path.iterdir():
        if file.is_file():
            assert secret.encode() not in file.read_bytes()


@pytest.mark.parametrize("edit_after_reservation", [False, True])
def test_bind_clears_only_the_submitted_revision(tmp_path, edit_after_reservation):
    with closing(Store(tmp_path / "draft.db")) as store:
        sid = store.create_session()
        drafts.save(store, sid, drafts.Save(expected_revision=0, text="Send me"))
        request = "draft_submit_request"
        submissions.reserve(store, request, sid, "Send me", {}, draft_revision=1)
        assert drafts.load(store, sid)["text"] == "Send me"
        if edit_after_reservation:
            drafts.save(store, sid, drafts.Save(expected_revision=1, text="Newer text"))
        submissions.bind(store, request)
        assert drafts.load(store, sid)["text"] == ("Newer text" if edit_after_reservation else "")
        _, created = submissions.reserve(store, request, sid, "Send me", {}, draft_revision=1)
        assert not created


def test_mismatched_draft_does_not_reserve_a_turn(tmp_path):
    with closing(Store(tmp_path / "draft.db")) as store:
        sid = store.create_session()
        drafts.save(store, sid, drafts.Save(expected_revision=0, text="Actual"))
        with pytest.raises(submissions.SubmissionError, match="no longer matches"):
            submissions.reserve(store, "draft_submit_request", sid, "Wrong", {}, draft_revision=1)
        assert drafts.load(store, sid)["text"] == "Actual"
        assert store.messages(sid) == []
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM turn_submissions").fetchone()[0] == 0
