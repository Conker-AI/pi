"""Guards on how the Inbox reads its inputs.

The approval queue is read once per inbox build and shared by both helpers, and
approving turns do not multiply database connections.
"""

from contextlib import closing

from pi.agents import inbox
from pi.conversation.store import Store


def _make_approval_turn(store, title):
    session_id = store.create_session(title=title)
    turn_id = store.start_turn(session_id)
    store.finish_turn(turn_id, "awaiting_approval")
    return turn_id


def test_inbox_reads_the_approval_queue_once(tmp_path, monkeypatch):
    """Three approval turns: awaiting_approval is called once and the items come back."""
    with closing(Store(tmp_path / "pi.db")) as store:
        turn_ids = [_make_approval_turn(store, f"Approval {index}") for index in range(1, 4)]

        original = store.awaiting_approval
        calls = []

        def counting_wrapper():
            calls.append(1)
            return original()

        monkeypatch.setattr(store, "awaiting_approval", counting_wrapper)
        result = inbox.inbox(store, now=1700000000.0)

        assert len(calls) == 1
        approvals = [item for item in result["needsYou"] if item["kind"] == "approval"]
        assert [item["id"] for item in approvals] == turn_ids


def _connections_for_inbox(store, monkeypatch):
    original = store._connect
    calls = []

    def counting_connect(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_connect", counting_connect)
    inbox.inbox(store, now=1700000000.0)
    monkeypatch.setattr(store, "_connect", original)
    return len(calls)


def test_more_approvals_do_not_open_more_connections(tmp_path, monkeypatch):
    """One approval turn and six: the inbox opens the same number of connections."""
    counts = []
    for index, turns in enumerate((1, 6)):
        with closing(Store(tmp_path / f"pi-{index}.db")) as store:
            for turn_index in range(turns):
                _make_approval_turn(store, f"Approval {turn_index}")
            counts.append(_connections_for_inbox(store, monkeypatch))

    assert counts[0] == counts[1]
