"""A queued deletion survives a passing failure, hides its chats and cannot be restored.

pi.conversation.forget_queue: a run that fails before cleanup starts stays queued for the next
start (bounded); the owner route puts every queued chat in the trash in the same step as the
queue write; restoring a queued chat is refused until the deletion has run.
"""

import subprocess
import threading
import time
from contextlib import closing

import pytest
from test_forget_queue import http as http
from test_forget_queue import owner
from test_forgetting import SECRET
from test_forgetting import conversation as conversation

from pi.conversation import archive, forget_queue
from pi.conversation.store import Store


class FailingForget:
    """The forget command fails like a busy file while `on`; the preview still runs for real."""

    def __init__(self, real):
        self.real, self.on = real, True

    def __call__(self, db, *args, timeout):
        if args[0] == "forget" and self.on:
            return subprocess.CompletedProcess(args, 1, "", "database is locked")
        return self.real(db, *args, timeout=timeout)


@pytest.fixture
def failing(monkeypatch):
    command = FailingForget(forget_queue._command)
    monkeypatch.setattr(forget_queue, "_command", command)
    return command


def test_a_passing_failure_keeps_the_chat_queued_until_a_later_start_forgets_it(
    conversation, failing
):
    path, root, child, *_ = conversation
    forget_queue.queue(path, [{"sessionId": root, "sessionIds": [root, child]}])
    assert forget_queue.run_pending(path) is True
    view = forget_queue.overview(path)
    assert view["pending"] == [root]
    assert [r["state"] for r in view["results"]] == ["retrying"]

    failing.on = False
    assert forget_queue.run_pending(path) is False
    view = forget_queue.overview(path)
    assert view["pending"] == [] and view["results"][0]["state"] == "done"
    with closing(Store(path)) as store:
        assert {m["content"] for m in store.messages(root)} == {None}


def test_a_failure_on_every_start_is_refused_after_the_last_attempt(conversation, failing):
    path, root, child, *_ = conversation
    forget_queue.queue(path, [{"sessionId": root, "sessionIds": [root, child]}])
    for _ in range(forget_queue.MAX_ATTEMPTS - 1):
        assert forget_queue.run_pending(path) is True
        assert forget_queue.overview(path)["pending"] == [root]
    assert forget_queue.run_pending(path) is False
    view = forget_queue.overview(path)
    assert view["pending"] == []
    [result] = view["results"]
    assert (result["state"], result["detail"]) == ("refused", "database is locked")
    with closing(Store(path)) as store:
        assert store.messages(root)[0]["content"] == SECRET


def test_queueing_again_starts_the_attempts_over(conversation, failing):
    path, root, child, *_ = conversation
    item = {"sessionId": root, "sessionIds": [root, child]}
    forget_queue.queue(path, [item])
    forget_queue.run_pending(path)
    forget_queue.queue(path, [item])
    assert forget_queue.pending_sessions(path) == {root, child}
    assert forget_queue._read(forget_queue._request_path(path)) == [item]


def test_the_route_hides_queued_chats_and_restoring_them_is_refused(
    http, conversation, monkeypatch
):
    path, root, child, *_ = conversation
    monkeypatch.setenv("PI_SUPERVISED", "true")
    store = http.app.state.store
    read = http.get("/forgetting/chats", params={"session": root}, headers=owner())
    [chat] = read.json()["chats"]
    body = {"chats": [{"session": root, "confirmation": chat["confirmation"]}]}
    assert http.post("/forgetting/chats", json=body, headers=owner()).status_code == 202
    with store._connect() as db:
        assert archive.trashed(db, [root, child]) == {root, child}

    with pytest.raises(archive.ArchiveError) as refused:
        archive.trash(store, archive.Trash(sessions=[child], trashed=False))
    assert refused.value.status == 409
    assert refused.value.detail["code"] == "deletion_pending"
    assert refused.value.detail["sessions"] == [child]
    with store._connect() as db:
        assert archive.trashed(db, [root, child]) == {root, child}  # nothing half restored

    # Once the queue is empty (the deletion was refused), the chats can be restored.
    forget_queue._write(forget_queue._request_path(path), [])
    archive.trash(store, archive.Trash(sessions=[root, child], trashed=False))
    with store._connect() as db:
        assert archive.trashed(db, [root, child]) == set()


def test_nothing_is_queued_when_hiding_fails(conversation):
    path, root, *_ = conversation

    def broken():
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        forget_queue.queue(path, [{"sessionId": root, "sessionIds": [root]}], before=broken)
    assert forget_queue.overview(path)["pending"] == []


def test_a_restart_that_fails_can_be_asked_for_again(monkeypatch):
    lock = threading.Lock()
    tried = threading.Event()
    monkeypatch.setattr(forget_queue, "_restart_once", lock)

    def hold(db):
        tried.set()
        raise OSError("cannot open")

    monkeypatch.setattr(forget_queue, "hold", hold)
    forget_queue.restart_soon(":memory:", delay=0, poll=0.01)
    assert tried.wait(5)
    deadline = time.monotonic() + 5
    while lock.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not lock.locked()
