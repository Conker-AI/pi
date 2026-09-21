from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from pi import drafts, forgetting, tasks
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
