"""Deleting chats forever from the owner's control plane (pi.conversation.forget_queue).

The route only queues: it checks the owner's confirmation against a fresh preview and asks for a
restart. The offline forgetting command, run in a child process before the Store opens, does
the forgetting. A chat whose sessions changed is refused, never widened; cleanup that started
and failed stays queued, so the Store refuses to start until it is finished.
"""

import hashlib
import json
import signal
import sqlite3
import threading
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_forgetting import SECRET
from test_forgetting import conversation as conversation

from pi.api import browser_contract
from pi.api import server as api
from pi.conversation import forget_queue
from pi.conversation.access import MaintenanceRequired
from pi.conversation.forgetting import preview
from pi.conversation.store import Store

OWNER = "synthetic-owner-key-" + "o" * 32


def files(path):
    return "".join(p.read_text(encoding="utf-8") for p in path.parent.glob("pi.db.forget-*.json"))


def test_preview_many_counts_what_a_deletion_reaches_and_names_unknown_chats(conversation):
    path, root, child, *_ = conversation
    plans = forget_queue.preview_many(path, [root, "ses_missing"])
    assert plans["ses_missing"]["error"].startswith("Session not found")
    view = forget_queue.summary(root, plans[root])
    assert view == {
        "sessionId": root,
        "sessionIds": sorted([root, child]),
        "messages": 2,
        "turns": 2,
        "approvalRequests": 1,
        "confirmation": preview(path, root)["confirmation"],
        "alreadyForgotten": False,
    }


def test_a_queued_chat_is_forgotten_before_the_store_opens(conversation):
    path, root, child, other, *_ = conversation
    forget_queue.queue(path, [{"sessionId": root, "sessionIds": [root, child]}])
    forget_queue.run_pending(path)
    view = forget_queue.overview(path)
    assert view["pending"] == []
    [result] = view["results"]
    assert result["sessionId"] == root and result["state"] == "done" and result["receiptId"]
    with closing(Store(path)) as store:
        assert {m["content"] for m in store.messages(root)} == {None}
        assert store.messages(other)[0]["content"] == "keep this"
    assert SECRET not in files(path)
    # Running again with nothing queued changes nothing.
    forget_queue.run_pending(path)
    assert forget_queue.overview(path)["results"] == [result]


def test_a_chat_that_grew_since_it_was_confirmed_is_refused_not_widened(conversation):
    path, root, *_ = conversation
    forget_queue.queue(path, [{"sessionId": root, "sessionIds": [root]}])
    forget_queue.run_pending(path)
    [result] = forget_queue.overview(path)["results"]
    assert (result["state"], result["detail"]) == ("refused", forget_queue.CHANGED)
    assert forget_queue.overview(path)["pending"] == []
    with closing(Store(path)) as store:
        assert store.messages(root)[0]["content"] == SECRET


def test_an_unknown_chat_is_refused_and_dropped(conversation):
    path, *_ = conversation
    forget_queue.queue(path, [{"sessionId": "ses_missing", "sessionIds": ["ses_missing"]}])
    forget_queue.run_pending(path)
    view = forget_queue.overview(path)
    assert view["pending"] == [] and view["results"][0]["state"] == "refused"


def test_unfinished_cleanup_keeps_the_chat_queued_and_stops(conversation):
    path, root, child, other, *_ = conversation
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO forgetting_maintenance VALUES (1, ?, ?)", (other, "0" * 64))
    forget_queue.queue(
        path,
        [
            {"sessionId": root, "sessionIds": [root, child]},
            {"sessionId": other, "sessionIds": [other]},
        ],
    )
    forget_queue.run_pending(path)
    view = forget_queue.overview(path)
    assert view["pending"] == [root, other]
    assert [r["state"] for r in view["results"]] == ["retrying"]
    with pytest.raises(MaintenanceRequired):
        Store(path)  # so the supervisor restarts Pi until the cleanup is finished
    with closing(sqlite3.connect(path)) as db:
        [content] = db.execute(
            "SELECT content FROM messages WHERE session_id=?", (root,)
        ).fetchone()
        assert SECRET in content


@pytest.fixture
def http(conversation, monkeypatch):
    path, *_ = conversation
    restarts = []
    state = api.app.state
    with closing(Store(path)) as store:
        monkeypatch.setattr(state, "store", store, raising=False)
        monkeypatch.setattr(state, "admin_key", "synthetic-admin-key-1234", raising=False)
        monkeypatch.setattr(
            state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
        )
        monkeypatch.setattr(state, "forget_restart", lambda: restarts.append(1), raising=False)
        client = TestClient(api.app)
        client.restarts = restarts
        yield client


def owner():
    return {"X-Pi-Owner-Key": OWNER}


def test_the_owner_route_queues_only_after_a_matching_confirmation(http, conversation, monkeypatch):
    path, root, child, *_ = conversation
    assert http.get("/forgetting/chats").status_code in (401, 403)
    empty = http.get("/forgetting/chats", headers=owner()).json()
    assert empty == {"supervised": False, "pending": [], "results": [], "chats": []}
    read = http.get("/forgetting/chats", params={"session": [root, root]}, headers=owner())
    [chat] = read.json()["chats"]
    assert chat["sessionIds"] == sorted([root, child]) and SECRET not in read.text
    body = {"chats": [{"session": root, "confirmation": chat["confirmation"]}]}

    unsupervised = http.post("/forgetting/chats", json=body, headers=owner())
    assert unsupervised.status_code == 409
    assert unsupervised.json()["detail"]["code"] == "not_supervised"

    monkeypatch.setenv("PI_SUPERVISED", "true")
    stale = {"chats": [{"session": root, "confirmation": "a" * 64}]}
    refused = http.post("/forgetting/chats", json=stale, headers=owner())
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "code": "scope_changed",
        "message": forget_queue.CHANGED,
        "sessions": [root],
    }
    missing = {"chats": [{"session": "ses_missing", "confirmation": "a" * 64}]}
    unknown = http.post("/forgetting/chats", json=missing, headers=owner())
    assert unknown.status_code == 404 and unknown.json()["detail"]["sessions"] == ["ses_missing"]
    twice = {"chats": [body["chats"][0], body["chats"][0]]}
    assert http.post("/forgetting/chats", json=twice, headers=owner()).status_code == 422
    assert http.restarts == [] and forget_queue.overview(path)["pending"] == []

    queued = http.post("/forgetting/chats", json=body, headers=owner())
    assert queued.status_code == 202, queued.text
    assert queued.json() == {"state": "restarting", "sessions": [root]}
    assert http.restarts == [1]
    view = http.get("/forgetting/chats", headers=owner()).json()
    assert view["supervised"] is True and view["pending"] == [root]
    assert http.app.state.store.messages(root)[0]["content"] == SECRET  # nothing forgotten yet


def test_a_chat_already_forgotten_is_done_without_a_restart(http, conversation, monkeypatch):
    path, root, *_ = conversation
    monkeypatch.setenv("PI_SUPERVISED", "1")
    http.app.state.store.close()
    forget_queue.queue(
        path, [{"sessionId": root, "sessionIds": preview(path, root)["session_ids"]}]
    )
    forget_queue.run_pending(path)
    with closing(Store(path)) as store:
        http.app.state.store = store
        [chat] = http.get("/forgetting/chats", params={"session": root}, headers=owner()).json()[
            "chats"
        ]
        assert chat["alreadyForgotten"] is True and chat["messages"] == 2
        body = {"chats": [{"session": root, "confirmation": chat["confirmation"]}]}
        done = http.post("/forgetting/chats", json=body, headers=owner())
    assert done.status_code == 200 and done.json() == {"state": "done", "sessions": [root]}
    assert http.restarts == []


def test_the_browser_asks_for_the_password_to_delete_forever():
    assert browser_contract.owner_allowed("GET", "/forgetting/chats")
    assert browser_contract.owner_allowed("POST", "/forgetting/chats")
    assert browser_contract.password_write("POST", "/forgetting/chats")
    assert not browser_contract.runtime_allowed("POST", "/forgetting/chats")
    assert not browser_contract.runtime_allowed("GET", "/forgetting/chats")


def test_restart_stops_the_process_once_and_a_watchdog_ends_a_stuck_shutdown(monkeypatch):
    killed, exited = threading.Event(), threading.Event()
    signals = []
    monkeypatch.setattr(forget_queue, "_restart_once", threading.Lock())
    monkeypatch.setattr(
        forget_queue.os, "kill", lambda pid, sig: (signals.append(sig), killed.set())
    )
    monkeypatch.setattr(forget_queue.os, "_exit", lambda code: exited.set())
    forget_queue.restart_soon(delay=0.01, grace=0.05)
    forget_queue.restart_soon(delay=0.01, grace=0.05)
    assert killed.wait(5) and exited.wait(5)
    assert signals == [signal.SIGTERM]


def test_queue_files_hold_ids_and_states_only(conversation):
    path, root, child, *_ = conversation
    forget_queue.queue(path, [{"sessionId": root, "sessionIds": [root, child]}])
    forget_queue.queue(path, [{"sessionId": root, "sessionIds": [root, child]}])
    assert json.loads(files(path)) == [{"sessionId": root, "sessionIds": [root, child]}]
