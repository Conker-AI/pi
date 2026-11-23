"""R-U3 step 3: finishing a memory correction or forget files an Inbox notice "answers used a
memory you changed". It holds ids only, counts its reach when read in chats that are visible, and
is hidden once nothing visible is left."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing, contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from pi.agents.inbox import inbox
from pi.api.browser_contract import owner_allowed
from pi.conversation import archive, recall_notices
from pi.conversation.forgetting import forget as forget_chat
from pi.conversation.forgetting import preview as preview_forget
from pi.conversation.store import Store
from pi.memory import edits
from pi.memory import forget as memory_forget
from pi.memory import store as memory_store
from pi.memory.corrections import Client

KEY = "synthetic-correction-capability-" + "k" * 32
WORDS = "private memory words of the owner"
ADMIN = {"X-Pi-Key": "synthetic-admin"}
OWNER = "synthetic-owner"


class Remote:
    """MemoryGate's correction and forget routes, enough for one memory."""

    def __init__(self):
        self.revision = 3
        self.refuse = None

    def handle(self, request):
        identity = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and "/memories/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": identity,
                    "agent_id": "companion",
                    "revision": self.revision,
                    "text": WORDS,
                    "source_type": "inferred",
                    "confidence": "low",
                },
            )
        body = json.loads(request.content)
        if self.refuse:
            return httpx.Response(self.refuse, json={})
        if "/forget/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "request_id": identity,
                    "memory_id": body["memory_id"],
                    "agent_id": "companion",
                    "revision": body["expected_revision"],
                    "status": "forgotten",
                    "index_removal": "removed",
                },
            )
        digest = edits.remote_digest("companion", edits.Apply(request_id=identity, **body))
        self.revision += 1
        return httpx.Response(
            200,
            json={
                "request_id": identity,
                "memory_id": body["memory_id"],
                "agent_id": "companion",
                "previous_revision": self.revision - 1,
                "revision": self.revision,
                "status": "applied",
                "payload_hash": digest,
                "indexing": "indexed",
            },
        )


@pytest.fixture
def setup(tmp_path):
    remote = Remote()
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        closing(
            Client(
                "http://memorygate", KEY, "companion", transport=httpx.MockTransport(remote.handle)
            )
        ) as client,
    ):
        yield store, client, remote


def use(store, session, revision=3, memory="mem-1"):
    store.append_message(session, "user", "hello")
    turn = store.start_turn(session)
    package = {"memories": [{"id": memory, "text": WORDS, "revision": revision}]}
    memory_store.save_context(store, turn, "ok", package)
    store.finish_turn(turn, "complete")
    return turn


def correct(store, client, request="correction_request_0001", revision=3):
    body = edits.Apply(
        request_id=request, memory_id="mem-1", expected_revision=revision, text="new words"
    )
    return edits.apply(store, client, body)


def items(store):
    return [i for i in inbox(store)["needsYou"] if i["kind"] == "memory-recall"]


def stored(store):
    with store._connect() as db:
        return [
            tuple(r) for r in db.execute("SELECT title, detail, session_id, facts FROM notices")
        ]


def test_a_correction_files_a_notice_with_counts_and_no_words(setup):
    store, client, _ = setup
    first, second = store.create_session(), store.create_session()
    use(store, first)
    use(store, first)
    use(store, second)
    assert items(store) == []
    assert correct(store, client)["status"] == "applied"
    [item] = items(store)
    assert (item["answers"], item["chats"], item["change"], item["memoryId"]) == (
        3,
        2,
        "corrected",
        "mem-1",
    )
    assert item["title"] == "A memory you corrected was used in 3 answers in 2 chats"
    assert set(item["sessionIds"]) == {first, second}
    rows = stored(store)
    assert len(rows) == 1 and rows[0][2] is None
    dumped = json.dumps(rows)
    assert WORDS not in dumped and first not in dumped and second not in dumped
    assert json.loads(rows[0][3]) == {"change": "corrected", "memoryId": "mem-1", "revision": 3}


def test_a_forget_files_a_notice(setup):
    store, client, _ = setup
    use(store, store.create_session())
    body = memory_forget.Forget(
        request_id="forget_request_0001", memory_id="mem-1", expected_revision=3
    )
    memory_forget.forget(store, client, body)
    [item] = items(store)
    assert (item["change"], item["answers"], item["chats"]) == ("forgotten", 1, 1)
    assert item["title"] == "A memory you forgot was used in 1 answer in 1 chat"


def test_no_use_files_nothing_and_a_refused_correction_files_nothing(setup):
    store, client, remote = setup
    correct(store, client, request="correction_request_0002")
    assert stored(store) == []
    use(store, store.create_session())
    remote.refuse = 409
    with pytest.raises(Exception):  # noqa: B017  (the refusal's own error type is not the point)
        correct(store, client, request="correction_request_0003")
    assert stored(store) == [] and items(store) == []


def test_only_answers_that_saw_the_old_text_count(setup):
    store, client, _ = setup
    old = store.create_session()
    use(store, old, revision=3)
    correct(store, client)
    use(store, store.create_session(), revision=4)
    [item] = items(store)
    assert (item["answers"], item["chats"]) == (1, 1) and item["sessionIds"] == [old]


def test_a_chat_that_leaves_drops_out_and_the_notice_goes_when_none_is_left(setup):
    store, client, _ = setup
    kept, trashed, gone = store.create_session(), store.create_session(), store.create_session()
    for session in (kept, trashed, gone):
        use(store, session)
    correct(store, client)
    assert items(store)[0]["chats"] == 3
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    [item] = items(store)
    assert item["chats"] == 2 and trashed not in item["sessionIds"]
    archive.trash(store, archive.Trash(sessions=[kept], trashed=True))
    assert [i["chats"] for i in items(store)] == [1]
    archive.trash(store, archive.Trash(sessions=[gone], trashed=True))
    assert items(store) == []
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=False))
    assert items(store)[0]["sessionIds"] == [trashed]


def test_forgetting_a_chat_takes_it_out_of_the_count(tmp_path):
    path = tmp_path / "pi.db"
    remote = Remote()
    transport = httpx.MockTransport(remote.handle)
    with closing(Client("http://memorygate", KEY, "companion", transport=transport)) as client:
        with closing(Store(path)) as store:
            kept, gone = store.create_session(), store.create_session()
            use(store, kept)
            use(store, gone)
            correct(store, client)
            assert items(store)[0]["chats"] == 2
        forget_chat(path, gone, preview_forget(path, gone)["confirmation"])
        with closing(Store(path)) as store:
            [item] = items(store)
            assert (item["answers"], item["chats"], item["sessionIds"]) == (1, 1, [kept])


def test_a_chat_in_the_trash_when_the_change_finishes_gets_its_notice_on_restore(setup):
    store, client, _ = setup
    trashed = store.create_session()
    use(store, trashed)
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    correct(store, client)
    assert items(store) == []
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=False))
    [item] = items(store)
    assert (item["answers"], item["sessionIds"]) == (1, [trashed])
    assert stored(store)[0][0] == "Memory changed"


def test_a_revision_that_is_not_an_int_files_nothing(setup):
    store, _, _ = setup
    use(store, store.create_session())
    with store._connect() as db:
        for bad in (None, "3", True):
            filed = recall_notices.file(
                db,
                request_id="bad_revision_00000001",
                memory_id="mem-1",
                change="corrected",
                revision=bad,  # type: ignore[arg-type]
            )
            assert filed is False
    assert stored(store) == []


def test_filing_twice_for_one_request_keeps_one_notice(setup):
    store, _, _ = setup
    use(store, store.create_session())
    with store._connect() as db:
        for _ in range(2):
            recall_notices.file(
                db,
                request_id="same_request_0000001",
                memory_id="mem-1",
                change="corrected",
                revision=3,
            )
    assert len(stored(store)) == 1


@pytest.fixture
def api(setup, monkeypatch):
    from pi.api import server

    store, _, _ = setup
    monkeypatch.setattr(server.app.state, "store", store, raising=False)
    monkeypatch.setattr(server.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        server.app.state,
        "owner_key_hash",
        hashlib.sha256(OWNER.encode()).hexdigest(),
        raising=False,
    )
    return TestClient(server.app)


def test_dismiss_hides_the_notice_once_and_a_bad_id_is_not_found(setup, api):
    store, client, _ = setup
    use(store, store.create_session())
    correct(store, client)
    [item] = items(store)
    path = f"/memory/recall-notices/{item['id']}/dismiss"
    assert owner_allowed("POST", path)
    assert api.post(path).status_code in {401, 403}
    first = api.post(path, headers=ADMIN)
    assert first.status_code == 200 and first.json()["changed"] is True
    assert items(store) == []
    assert api.post(path, headers=ADMIN).json()["changed"] is False
    missing = api.post(f"/memory/recall-notices/mrn_{'0' * 32}/dismiss", headers=ADMIN)
    assert missing.status_code == 404
    other = api.post(f"/memory/recall-notices/plan_{'0' * 32}/dismiss", headers=ADMIN)
    assert other.status_code == 422


def test_a_second_change_counts_only_answers_not_already_notified(setup):
    store, client, remote = setup
    use(store, store.create_session(), revision=3)
    correct(store, client)
    remote.revision = 4
    [first] = items(store)
    recall_notices.dismiss(store, first["id"])
    # No answer used revision 4: a second change has nothing new to tell.
    correct(store, client, request="correction_request_0002", revision=4)
    assert items(store) == [] and len(stored(store)) == 1
    later = store.create_session()
    remote.revision = 5
    use(store, later, revision=5)
    correct(store, client, request="correction_request_0003", revision=5)
    [second] = items(store)
    assert (second["answers"], second["chats"], second["sessionIds"]) == (1, 1, [later])


def test_dead_notices_never_hide_an_older_one_that_reaches_a_visible_chat(setup):
    store, _, _ = setup
    visible = store.create_session()
    use(store, visible, memory="mem-live")
    with store._connect() as db:
        recall_notices.file(
            db,
            request_id="live_request_00000001",
            memory_id="mem-live",
            change="corrected",
            revision=3,
            now=time.time() + 1,
        )
    dead = store.create_session()
    for index in range(recall_notices.MAX_OPEN + 1):
        use(store, dead, memory=f"mem-dead-{index}")
    with store._connect() as db:
        for index in range(recall_notices.MAX_OPEN + 1):
            recall_notices.file(
                db,
                request_id=f"dead_request_{index:08d}",
                memory_id=f"mem-dead-{index}",
                change="corrected",
                revision=3,
                now=time.time() + 10 + index,
            )
    archive.trash(store, archive.Trash(sessions=[dead], trashed=True))
    assert [i["memoryId"] for i in recall_notices.needs_you(store)] == ["mem-live"]


def history(store, session, *, uses, earlier, open_notices=3, new=2):
    """`uses` old uses of mem-1 at revision 1, counted by `earlier` dismissed notices, then
    `open_notices` open notices that each count `new` uses of their own revision."""
    start = time.time() - 10_000
    old = [(f"old-{i}", 1, start + i * 1e-3) for i in range(uses)]
    fresh = [
        (f"new-{k}-{i}", k + 2, start + 1_000 + k * 10 + i)
        for k in range(open_notices)
        for i in range(new)
    ]
    with store._connect() as db:
        db.execute("PRAGMA foreign_keys=OFF")  # use rows of turns this test does not need
        db.executemany(
            "INSERT INTO memory_uses VALUES (?,?,'mem-1',?,'context',?)",
            [(turn, session, revision, at) for turn, revision, at in old + fresh],
        )
        filed = [(1, start + 500 + k, start) for k in range(earlier)]
        filed += [(k + 2, start + 1_000 + k * 10 + new, None) for k in range(open_notices)]
        for index, (revision, at, dismissed) in enumerate(filed):
            db.execute(
                "INSERT INTO notices(id,kind,subject,title,facts,created_at,dismissed_at) "
                "VALUES (?,'memory-recall',?,'x',?,?,?)",
                (
                    f"mrn_{index:032x}",
                    f"history_{index}",
                    json.dumps({"memoryId": "mem-1", "revision": revision}),
                    at,
                    dismissed,
                ),
            )


def read_cost(store, monkeypatch):
    """needs_you, the SQL statements it ran and its SQLite VM steps (in hundreds)."""
    seen: list[str] = []
    steps = [0]

    def step() -> int:
        steps[0] += 1
        return 0

    original = store._connect

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(seen.append)
            db.set_progress_handler(step, 100)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connect", traced)
        found = recall_notices.needs_you(store)
    return found, len(seen), steps[0]


def test_an_inbox_read_reads_only_the_uses_its_open_notices_may_count(tmp_path, monkeypatch):
    costs = []
    for uses, earlier in ((50, 5), (5_000, 5)):
        with closing(Store(tmp_path / f"pi-{uses}.db")) as store:
            history(store, store.create_session(), uses=uses, earlier=earlier)
            found, statements, steps = read_cost(store, monkeypatch)
            assert [i["answers"] for i in found] == [2, 2, 2]
            costs.append((statements, steps))
    (few_statements, few_steps), (many_statements, many_steps) = costs
    # 100 times the history already told: the same statements, and no more rows read.
    assert many_statements == few_statements
    assert many_steps <= few_steps + 2, costs


def test_a_clock_step_back_never_counts_a_use_twice(setup):
    store, _, _ = setup
    session, base = store.create_session(), time.time()
    for revision, at in ((3, 10), (4, 20), (5, 250)):
        use(store, session, revision=revision)
        with store._connect() as db:
            db.execute(
                "UPDATE memory_uses SET recorded_at=? WHERE revision=?", (base + at, revision)
            )
    # Filed in this order; the clock stepped back between the first two.
    with store._connect() as db:
        for name, revision, at in (("first", 3, 200), ("second", 4, 100), ("third", 5, 300)):
            assert recall_notices.file(
                db,
                request_id=f"clock_{name}_request_01",
                memory_id="mem-1",
                change="corrected",
                revision=revision,
                now=base + at,
            )
    assert sorted(i["answers"] for i in items(store)) == [1, 2]  # three uses, each told once


def test_an_answer_saved_after_its_change_is_told_once_by_the_next_change(setup):
    store, client, remote = setup
    use(store, store.create_session(), revision=3)
    correct(store, client)
    [first] = items(store)
    # A turn that read revision 3 before the correction saved its recall after it finished.
    late = store.create_session()
    use(store, late, revision=3)
    assert items(store) == [first]  # a filed notice keeps the answers it was filed for
    recall_notices.dismiss(store, first["id"])
    remote.revision = 4
    correct(store, client, request="correction_request_0002", revision=4)
    [second] = items(store)
    assert (second["answers"], second["chats"], second["sessionIds"]) == (1, 1, [late])
    remote.revision = 5
    correct(store, client, request="correction_request_0003", revision=5)
    assert items(store) == [second] and len(stored(store)) == 2  # told once, not again


def test_a_notice_with_bad_facts_is_never_shown_and_bounds_nothing(setup):
    store, client, _ = setup
    use(store, store.create_session())
    correct(store, client)
    recall_notices.dismiss(store, items(store)[0]["id"])
    late = store.create_session()
    use(store, late)  # saved after the correction
    with store._connect() as db:
        bad = [{"memoryId": "mem-1", "revision": "4"}, {"memoryId": {"id": 1}, "revision": 4}, {}]
        for index, facts in enumerate([*bad, {"memoryId": "mem-1", "revision": 4}]):
            db.execute(
                "INSERT INTO notices(id,kind,subject,title,facts,created_at) VALUES (?,?,?,?,?,?)",
                (
                    f"mrn_{index:032x}",
                    "memory-recall",
                    f"bad_{index}",
                    "x",
                    json.dumps(facts),
                    time.time() + 1 + index,
                ),
            )
    # Only the valid one shows, with the late answer: not the dismissed one again, and not
    # lost behind the bad ones filed after it was saved.
    [item] = items(store)
    assert (item["id"], item["answers"], item["sessionIds"]) == (f"mrn_{3:032x}", 1, [late])


def test_a_use_of_a_newer_revision_is_not_lost_when_its_time_is_older(setup):
    store, _, _ = setup
    session = store.create_session()
    use(store, session, revision=3)
    with store._connect() as db:
        recall_notices.file(
            db, request_id="clock_request_000001", memory_id="mem-1", change="corrected", revision=3
        )
        [(filed,)] = db.execute("SELECT created_at FROM notices").fetchall()
    use(store, session, revision=4)
    with store._connect() as db:  # the clock stepped back before this use was saved
        db.execute("UPDATE memory_uses SET recorded_at=? WHERE revision=4", (filed - 5,))
        recall_notices.file(
            db, request_id="clock_request_000002", memory_id="mem-1", change="corrected", revision=4
        )
    assert [i["answers"] for i in items(store)] == [1, 1]
