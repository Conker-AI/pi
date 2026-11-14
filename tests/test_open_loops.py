"""Open loops (R-M7): promises across chats, their order and paging, state changes, and the
visibility rule (a forgotten or trashed chat's loops are neither listed nor changed)."""

from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import date

import pytest
from fastapi.testclient import TestClient

from pi.api.browser_contract import owner_allowed
from pi.context import open_loops as loops
from pi.conversation import archive
from pi.conversation.store import Store

ADMIN = {"X-Pi-Key": "synthetic-admin"}
OWNER = "synthetic-owner"
PAST, SOON, LATER = "2000-01-01", "2999-01-01", "2999-06-01"


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


@pytest.fixture
def client(store, monkeypatch):
    from pi.api import server as api

    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    return TestClient(api.app)


def add(store, session, text="Send the report", **fields):
    return loops.add(store, loops.LoopAdd(session_id=session, text=text, **fields))["loop"]


def ids(page):
    return [item["id"] for item in page["items"]]


def listing(store, state="open", due_before=None, limit=50, after=None):
    return loops.listing(store, state, due_before, limit, after)


def set_status(store, session, status):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE sessions SET status=? WHERE id=?", (status, session))
        db.commit()


def test_a_loop_quotes_its_message_and_validates_against_the_reply_model(store):
    session = store.create_session(title="Work")
    message = store.append_message(session, "user", "I'll send the report on Friday")
    loop = add(store, session, "  Send the report  ", due_on=SOON, message_id=message["id"])
    assert loop["text"] == "Send the report" and loop["quote"] == "I'll send the report on Friday"
    assert loop["sessionTitle"] == "Work" and loop["promisedBy"] == "owner"
    assert (loop["dueOn"], loop["state"], loop["revision"], loop["overdue"]) == (
        "2999-01-01",
        "open",
        1,
        False,
    )
    page = listing(store)
    loops.LoopPage.model_validate(page)
    assert ids(page) == [loop["id"]] and page["timeZone"] and page["nextAfter"] is None
    assert page["today"] == date.fromisoformat(page["today"]).isoformat()


def test_soonest_due_first_undated_last_and_overdue_marked(store):
    session = store.create_session()
    undated = add(store, session, "Undated")
    later = add(store, session, "Later", due_on=LATER, promised_by="conker")
    late = add(store, session, "Late", due_on=PAST)
    soon = add(store, session, "Soon", due_on=SOON)
    page = listing(store)
    assert ids(page) == [late["id"], soon["id"], later["id"], undated["id"]]
    assert [item["overdue"] for item in page["items"]] == [True, False, False, False]
    assert page["items"][2]["promisedBy"] == "conker"
    assert ids(listing(store, due_before=date(2999, 6, 1))) == [late["id"], soon["id"]]


def test_pages_walk_every_loop_once_and_a_bad_cursor_is_refused(store):
    session = store.create_session()
    made = [add(store, session, f"Promise {n}", due_on=SOON if n % 2 else None) for n in range(5)]
    seen, after = [], None
    while True:
        page = listing(store, limit=2, after=after)
        seen += ids(page)
        after = page["nextAfter"]
        if after is None:
            break
    assert sorted(seen) == sorted(item["id"] for item in made) and len(seen) == 5
    with pytest.raises(loops.LoopError) as refused:
        listing(store, after="not-a-loop")
    assert (refused.value.code, refused.value.status) == ("invalid_cursor", 422)


def test_a_cursor_whose_loop_was_forgotten_says_to_start_again(store):
    kept, forgotten = store.create_session(), store.create_session()
    add(store, forgotten, due_on=SOON)
    add(store, kept, due_on=LATER)
    page = listing(store, limit=1)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        loops.redact(db, [forgotten])
        db.commit()
    with pytest.raises(loops.LoopError) as gone:
        listing(store, after=page["nextAfter"])
    assert (gone.value.code, gone.value.status) == ("cursor_gone", 422)


def test_done_dropped_and_open_again_also_in_a_forked_or_closed_chat(store):
    session = store.create_session()
    loop = add(store, session)
    set_status(store, session, "forked")
    done = loops.change_state(store, loop["id"], loops.LoopState(state="done"))["loop"]
    assert (done["state"], done["revision"]) == ("done", 2)
    assert listing(store)["items"] == [] and ids(listing(store, "done")) == [loop["id"]]
    set_status(store, session, "closed")
    again = loops.change_state(store, loop["id"], loops.LoopState(state="done"))["loop"]
    assert again["revision"] == 2  # the same state again changes nothing
    reopened = loops.change_state(
        store, loop["id"], loops.LoopState(state="open", expected_revision=2)
    )["loop"]
    assert (reopened["state"], reopened["revision"]) == ("open", 3)
    dropped = loops.change_state(store, loop["id"], loops.LoopState(state="dropped"))["loop"]
    assert ids(listing(store, "all")) == [dropped["id"]] and dropped["state"] == "dropped"


def test_a_stale_revision_or_an_unknown_loop_is_refused(store):
    loop = add(store, store.create_session())
    with pytest.raises(loops.LoopError) as stale:
        loops.change_state(store, loop["id"], loops.LoopState(state="done", expected_revision=5))
    assert (stale.value.code, stale.value.status, stale.value.detail["revision"]) == (
        "revision_conflict",
        409,
        1,
    )
    for loop_id in ("loop_" + "f" * 32, "dec_0123456789abcdef"):
        with pytest.raises(loops.LoopError) as missing:
            loops.change_state(store, loop_id, loops.LoopState(state="done"))
        assert missing.value.status == 404
    assert listing(store)["items"][0]["revision"] == 1


def test_adding_refuses_bad_text_another_chats_message_and_hidden_chats(store):
    session, other = store.create_session(), store.create_session()
    elsewhere = store.append_message(other, "user", "Not here")
    for text in ("   ", "tab\there"):
        with pytest.raises(loops.LoopError) as refused:
            add(store, session, text)
        assert (refused.value.code, refused.value.status) == ("invalid_text", 422)
    with pytest.raises(loops.LoopError) as foreign:
        add(store, session, message_id=elsewhere["id"])
    assert (foreign.value.code, foreign.value.status) == ("outside_boundary", 422)
    for due in ("2026-02-30", loops.UNDATED):  # the sentinel would sort as undated
        with pytest.raises(loops.LoopError) as impossible:
            add(store, session, due_on=due)
        assert (impossible.value.code, impossible.value.status) == ("invalid_date", 422)
    assert add(store, session, due_on="9999-12-30")["dueOn"] == "9999-12-30"
    archive.trash(store, archive.Trash(sessions=[other], trashed=True))
    set_status(store, session, "forgotten")
    for target in (other, session, "ses_0000000000000000"):
        with pytest.raises(loops.LoopError) as hidden:
            add(store, target)
        assert hidden.value.status == 404
    assert listing(store, "all")["items"] == []


def test_a_trashed_chats_loops_are_hidden_until_it_is_restored(store):
    kept, trashed = store.create_session(), store.create_session()
    shown = add(store, kept, due_on=SOON)
    hidden = add(store, trashed, due_on=PAST)
    child = store.create_session(parent_id=trashed)
    in_branch = add(store, child, "In a branch")
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    assert ids(listing(store, "all")) == [shown["id"]]
    for loop_id in (hidden["id"], in_branch["id"]):
        with pytest.raises(loops.LoopError) as refused:
            loops.change_state(store, loop_id, loops.LoopState(state="done"))
        assert refused.value.status == 404
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=False))
    assert ids(listing(store, "all")) == [hidden["id"], shown["id"], in_branch["id"]]


def test_the_open_cap_counts_only_open_loops(store, monkeypatch):
    monkeypatch.setattr(loops, "MAX_OPEN", 2)
    session = store.create_session()
    first, _ = add(store, session), add(store, session)
    with pytest.raises(loops.LoopError) as full:
        add(store, session)
    assert (full.value.code, full.value.status) == ("too_many", 409)
    loops.change_state(store, first["id"], loops.LoopState(state="done"))
    assert add(store, session)["state"] == "open"
    with pytest.raises(loops.LoopError) as reopened:
        loops.change_state(store, first["id"], loops.LoopState(state="open"))
    assert (reopened.value.code, reopened.value.status) == ("too_many", 409)
    assert loops.change_state(store, first["id"], loops.LoopState(state="dropped"))


def test_the_open_cap_counts_only_loops_the_owner_can_see(store, monkeypatch):
    monkeypatch.setattr(loops, "MAX_OPEN", 2)
    trashed, kept = store.create_session(), store.create_session()
    add(store, trashed), add(store, trashed)
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    first = add(store, kept)
    add(store, kept)
    with pytest.raises(loops.LoopError) as full:
        add(store, kept)
    assert (full.value.code, full.value.status) == ("too_many", 409)
    loops.change_state(store, first["id"], loops.LoopState(state="done"))
    assert loops.change_state(store, first["id"], loops.LoopState(state="open"))["loop"]


def test_routes_names_models_and_keys(store, client):
    session = store.create_session(title="Work")
    added = client.post(
        "/open-loops",
        json={"session_id": session, "text": "Call the bank", "due_on": "2999-01-01"},
        headers=ADMIN,
    )
    assert added.status_code == 200
    loops.LoopReceipt.model_validate(added.json())
    loop_id = added.json()["loop"]["id"]
    page = client.get("/open-loops?state=open&due_before=2999-02-01&limit=5", headers=ADMIN)
    assert page.status_code == 200 and ids(page.json()) == [loop_id]
    owner = {"X-Pi-Owner-Key": OWNER}
    assert client.get("/open-loops", headers=owner).status_code == 200
    # Writes stay with the recovery key until the dashboard has a screen for them.
    refused = client.post(f"/open-loops/{loop_id}/state", json={"state": "done"}, headers=owner)
    assert refused.status_code == 403
    assert client.get("/open-loops").status_code == 401
    done = client.post(f"/open-loops/{loop_id}/state", json={"state": "done"}, headers=ADMIN)
    assert done.status_code == 200 and done.json()["loop"]["state"] == "done"
    for query in ("state=gone", "limit=0", "limit=101", "due_before=friday"):
        assert client.get("/open-loops?" + query, headers=ADMIN).status_code == 422
    extra = {"session_id": session, "text": "x", "extra": 1}
    assert client.post("/open-loops", json=extra, headers=ADMIN).status_code == 422
    assert owner_allowed("GET", "/open-loops") and not owner_allowed("POST", "/open-loops")
    schema = client.get("/openapi.json").json()["paths"]
    for path, method, model in (
        ("/open-loops", "get", "LoopPage"),
        ("/open-loops", "post", "LoopReceipt"),
        ("/open-loops/{loop_id}/state", "post", "LoopReceipt"),
    ):
        reply = schema[path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert reply == {"$ref": f"#/components/schemas/{model}"}
