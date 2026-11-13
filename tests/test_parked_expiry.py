"""ToolGate keeps an approval it let expire as pending, so Pi ends the parked turn itself: the
chat says nothing was done, the Inbox lists it under Handled, and nothing waits for ever."""

import json
from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_decision_sweep import Statuses
from test_inbox_answers import parked

from pi.agents import inbox
from pi.conversation.store import Store
from pi.turns import answers, decision_sweep

EXPIRES = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
AT = EXPIRES.timestamp()
ISO = EXPIRES.isoformat()


@pytest.fixture()
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        yield store


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    monkeypatch.setattr(answers, "background", lambda work: work())


def expiring(store, turn, value=ISO):
    with store._connect() as db:
        db.execute("UPDATE turns SET approval_expires_at=? WHERE id=?", (value, turn))


def notes(store, session):
    with store._connect() as db:
        return [
            json.loads(json.loads(row["content"]))
            for row in db.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='tool' ORDER BY seq",
                (session,),
            )
        ]


def test_an_expired_approval_ends_the_turn_and_says_so_in_the_inbox(store):
    loop, gate, session, turn, _ = parked(store, ["unused"])
    expiring(store, turn)
    toolgate = Statuses()
    # Expired, but not yet past the grace an answer given just before it may still need.
    decision_sweep.sweep(store, toolgate, loop, now=AT + decision_sweep.EXPIRY_GRACE - 1)
    assert store.get_turn(turn)["status"] == "awaiting_approval"
    decision_sweep.sweep(store, toolgate, loop, now=AT + decision_sweep.EXPIRY_GRACE)
    saved = store.get_turn(turn)
    assert (saved["status"], saved["detail"]) == ("cancelled", answers.EXPIRED)
    assert notes(store, session)[-1] == {"tool": "t_echo", "declined": answers.EXPIRED_NOTE}
    assert len(gate.invocations) == 1
    handled = [item for item in inbox.inbox(store)["handled"] if item["kind"] == "approval-expired"]
    assert handled == [
        {
            "kind": "approval-expired",
            "title": "Approval expired: t_echo",
            "chat": "Conversation",
            "at": saved["ended_at"],
            "source": {"kind": "turn", "sessionId": session, "turnId": turn},
        }
    ]
    assert not inbox.inbox(store)["needsYou"]


def test_expiry_works_without_toolgate_and_never_guesses_a_bad_time(store):
    loop, _, _, turn, _ = parked(store, ["unused"])
    for value in ("not a time", "2026-11-13T12:00:00"):
        expiring(store, turn, value)
        decision_sweep.sweep(store, None, loop, now=AT + 10**6)
        assert store.get_turn(turn)["status"] == "awaiting_approval"
    expiring(store, turn)
    decision_sweep.sweep(store, None, loop, now=AT + 10**6)
    assert store.get_turn(turn)["detail"] == answers.EXPIRED


def test_a_decision_seen_before_the_expiry_is_acted_on_not_expired(store):
    loop, gate, _, turn, request = parked(store, ["Echoed."])
    expiring(store, turn)
    toolgate, seen = Statuses(**{request: "approved"}), {}
    late = AT + decision_sweep.EXPIRY_GRACE
    # Seen decided in this pass, acted on after its own grace: never ended as expired first.
    assert decision_sweep.sweep(store, toolgate, loop, seen=seen, now=late) == 0
    assert store.get_turn(turn)["status"] == "awaiting_approval"
    assert (
        decision_sweep.sweep(store, toolgate, loop, seen=seen, now=late + decision_sweep.GRACE) == 1
    )
    assert store.get_turn(turn)["status"] == "complete"
    assert gate.invocations[-1][2] == request


def test_a_hidden_chat_leaves_no_expired_line(store):
    loop, _, session, turn, _ = parked(store, ["unused"])
    expiring(store, turn)
    decision_sweep.sweep(store, None, loop, now=AT + 10**6)
    assert [i for i in inbox.inbox(store)["handled"] if i["kind"] == "approval-expired"]
    with store._connect() as db:
        db.execute("INSERT INTO session_trash VALUES (?, ?)", (session, AT))
    assert not [i for i in inbox.inbox(store)["handled"] if i["kind"] == "approval-expired"]
