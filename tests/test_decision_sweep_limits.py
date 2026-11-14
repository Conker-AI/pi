"""The sweep's limits hold with many requests waiting, expired tool cron runs end like expired
turns, and his Change words given after Pi ended the turn are kept, never dropped."""

from contextlib import closing, suppress
from datetime import UTC, datetime

import pytest
from test_decision_sweep import Statuses
from test_inbox_answers import inputs, parked
from test_jobs import definition

from pi.conversation import drafts
from pi.conversation.store import Store
from pi.schedule import jobs
from pi.turns import answers, decision_sweep

EXPIRES = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
AT = EXPIRES.timestamp()
LATE = AT + decision_sweep.EXPIRY_GRACE


@pytest.fixture()
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        yield store


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    monkeypatch.setattr(answers, "background", lambda work: work())


def expiring(store, turn, value):
    with store._connect() as db:
        db.execute("UPDATE turns SET approval_expires_at=? WHERE id=?", (value, turn))


def waiting_run(store, request_id, expires_at):
    job = jobs.create(store, definition(), now=0)
    run = jobs.run_now(store, job["id"], "decision_sweep_" + request_id, now=1)
    receipt = {"status": "awaiting_approval", "request_id": request_id, "expires_at": expires_at}
    jobs.dispatch_claim(store, run, lambda *a, **kw: receipt)
    return job, run


def withdrawals(store):
    with store._connect() as db:
        return [row[0] for row in db.execute("SELECT request_id FROM approval_withdrawals")]


def test_an_expired_tool_cron_run_ends_and_frees_its_job(store):
    job, run = waiting_run(store, "tg-late", EXPIRES.isoformat())
    loop = type("Loop", (), {"store": store})()
    # Not yet past the grace, or ToolGate unreadable: the run keeps waiting.
    decision_sweep.sweep(store, Statuses(), loop, now=LATE - 1)
    decision_sweep.sweep(store, Statuses(**{"tg-late": None}), loop, now=LATE + 10**6)
    assert jobs.get_run(store, run["id"])["status"] == "awaiting_approval"
    decision_sweep.sweep(store, Statuses(), loop, now=LATE)
    assert jobs.get_run(store, run["id"])["status"] == "cancelled"
    # Its expired card leaves ToolGate's Inbox, and the job's next run is no longer held.
    assert withdrawals(store) == ["tg-late"]
    assert not jobs.run_now(store, job["id"], "decision_sweep_next_run", now=2)["replayed"]


@pytest.mark.parametrize("value", [None, "not a time", "2026-11-13T12:00:00"])
def test_a_tool_cron_run_without_a_readable_expiry_is_never_guessed(store, value):
    _, run = waiting_run(store, "tg-odd", value)
    loop = type("Loop", (), {"store": store})()
    decision_sweep.sweep(store, Statuses(), loop, now=LATE + 10**6)
    assert jobs.get_run(store, run["id"])["status"] == "awaiting_approval"


def test_an_expiry_is_not_starved_by_older_turns_that_never_end(store):
    loop, _, _, old, _ = parked(store, ["unused"])
    expiring(store, old, "2026-11-13T12:00:00")  # zone-less: waits for ever by design
    _, _, _, new, _ = parked(store, ["unused"])
    expiring(store, new, EXPIRES.isoformat())
    with store._connect() as db:
        db.execute("UPDATE turns SET approval_request_id='tg-new' WHERE id=?", (new,))
    for now in range(int(LATE), int(LATE) + 20 * 60, 60):
        decision_sweep.sweep(store, Statuses(), loop, limit=1, now=now)
    assert store.get_turn(new)["detail"] == answers.EXPIRED
    assert store.get_turn(old)["status"] == "awaiting_approval"


def test_tries_hold_when_more_wait_than_one_pass_reads(store, monkeypatch):
    turns = [parked(store, ["unused"])[3] for _ in range(2)]
    requests = ["tg-first", "tg-second"]
    with store._connect() as db:
        for turn, request in zip(turns, requests, strict=True):
            db.execute("UPDATE turns SET approval_request_id=? WHERE id=?", (request, turn))
    tries = []

    class Stuck:
        def __init__(self):
            self.store = store

        def resume_turn(self, turn_id):
            tries.append(turn_id)
            raise RuntimeError("the tool is not available")

    def swallow(work):
        with suppress(RuntimeError):
            work()

    monkeypatch.setattr(answers, "background", swallow)
    seen, toolgate = {}, Statuses(**dict.fromkeys(requests, "approved"))
    for now in range(0, 400 * 60, 60):
        decision_sweep.sweep(store, toolgate, Stuck(), limit=1, seen=seen, now=now)
    assert sorted(tries) == sorted(turns * decision_sweep.TRIES)


def test_his_change_words_after_the_turn_expired_are_kept_as_his_draft(store):
    loop, _, session, turn, request = parked(store, ["unused"])
    expiring(store, turn, EXPIRES.isoformat())
    decision_sweep.sweep(store, Statuses(), loop, now=LATE)
    assert store.get_turn(turn)["detail"] == answers.EXPIRED
    before = inputs(store, session)
    # ToolGate still takes a "no" after the expiry; the gateway brings his words.
    result = answers.decided(loop, request, "rejected", "Send it to Anna instead.")
    assert result["continued"] == "kept_as_draft"
    assert drafts.load(store, session)["text"] == "Send it to Anna instead."
    # Kept, never sent: they reach a model only when he sends them himself.
    assert inputs(store, session) == before


def test_a_late_no_without_words_or_an_unknown_request_keeps_nothing(store):
    loop, _, session, turn, request = parked(store, ["unused"])
    expiring(store, turn, EXPIRES.isoformat())
    decision_sweep.sweep(store, Statuses(), loop, now=LATE)
    assert answers.decided(loop, request, "rejected", "  ") == {"continued": "nothing_waiting"}
    assert answers.decided(loop, "tg-unknown", "rejected", "Hello.") == {
        "continued": "nothing_waiting"
    }
    assert answers.decided(loop, request, "approved", "Hello.") == {"continued": "nothing_waiting"}
    assert drafts.load(store, session)["text"] == ""
