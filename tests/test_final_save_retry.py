"""The final answer save failing while Pi keeps running, with the real store on disk.

The database fails inside the final commit, after the answer row is written, so each failed try
rolls back the way a real write error does. The answer is tried again a few times, then waits and
is saved once the database works again, without a restart. A stop or a steer that arrives
meanwhile still refuses it, a save the database itself refuses ends the turn in plain words, and
nothing is ever saved, asked of a model, or run twice.
"""

import sqlite3
from contextlib import closing

import pytest
from test_loop import Recorder, loop_with
from test_restart_points import Stop, held, restarted

from pi.conversation import submissions
from pi.conversation.store import Store
from pi.turns import control as turn_control
from pi.turns import restart, steering
from pi.turns.failures import ActedWithoutReply, TurnFailed

REQUEST = "save_retry_request_0001"
START = restart._start


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    """No real waits between tries (each is recorded), and the settle runs only when a test asks."""
    seen = []
    monkeypatch.setattr(restart, "_waiting", {})
    monkeypatch.setattr(restart, "_pause", seen.append)
    monkeypatch.setattr(restart, "_start", lambda: None)
    return seen


class Faults:
    """The database failing at the final save: the answer row is written, then the write fails and
    the whole commit rolls back. Fails `left` more times; `float("inf")` until told otherwise."""

    def __init__(self, monkeypatch, left):
        self.left, self.tries = left, 0
        real = submissions.append

        def append(db, *args, **kwargs):
            message = real(db, *args, **kwargs)
            if kwargs.get("purpose") == "final":
                self.tries += 1
                if self.left > 0:
                    self.left -= 1
                    raise sqlite3.OperationalError("disk I/O error")
            return message

        monkeypatch.setattr(submissions, "append", append)


class Answers(Recorder):
    """Answers in order, one per model call."""

    def __init__(self, *replies):
        super().__init__()
        self.replies = list(replies)

    def complete(self, messages, *, model):
        self.reply = self.replies[min(len(self.calls), len(self.replies) - 1)]
        return super().complete(messages, model=model)


def contents(store, session):
    return [(m["role"], m["content"]) for m in store.messages(session)]


def running(store, session):
    return next(t["id"] for t in store.turns(session) if t["status"] == "running")


def test_a_save_that_fails_twice_is_saved_on_the_third_try(tmp_path, monkeypatch, pauses):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        faults, provider = Faults(monkeypatch, 2), Recorder("the answer")
        result = loop_with(store, provider).run_turn(session, "hello", request_id=REQUEST)
        assert result["message"]["content"] == "the answer"
        assert faults.tries == 3 and pauses == [0.25, 1.0]
        assert contents(store, session) == [("user", "hello"), ("assistant", "the answer")]
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "complete"
        assert [ref["purpose"] for ref in receipt["message_refs"]] == ["input", "final"]
        assert len(provider.calls) == 1 and held(store) == 0 and restart.waiting() == []


def test_a_save_that_keeps_failing_waits_and_is_saved_once_the_database_works(
    tmp_path, monkeypatch, pauses
):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        faults, provider = Faults(monkeypatch, float("inf")), Recorder("the answer")
        loop = loop_with(store, provider)
        with pytest.raises(TurnFailed, match="could not save the answer yet"):
            loop.run_turn(session, "hello", request_id=REQUEST)
        assert faults.tries == 4 and pauses == list(restart.RETRY_DELAYS)
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "running" and receipt["final_message_id"] is None
        assert held(store) == 1 and restart.waiting() == [receipt["turn_id"]]

        assert restart.settle() == 0  # Still failing: it keeps waiting.
        assert restart.waiting() == [receipt["turn_id"]]
        faults.left = 0  # The database works again.
        assert restart.settle() == 1
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "complete"
        assert contents(store, session) == [("user", "hello"), ("assistant", "the answer")]
        turn = store.get_turn(receipt["turn_id"])
        assert turn["model"] == "test-model" and turn["input_tokens"] == 11
        assert held(store) == 0 and restart.waiting() == [] and restart.settle() == 0

        replay = loop.run_turn(session, "hello", request_id=REQUEST)
        assert replay["message"]["content"] == "the answer" and len(provider.calls) == 1
        again = loop.run_turn(session, "next", request_id="save_retry_request_0002")
        assert again["message"]["content"] == "the answer"  # The chat is free.


def test_the_settle_runs_by_itself_while_an_answer_waits(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(restart, "_start", lambda: started.append(START()) or started[-1])
    monkeypatch.setattr(restart, "SETTLE_SECONDS", 0.01)
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        Faults(monkeypatch, len(restart.RETRY_DELAYS) + 1)  # Every live try; the settle's works.
        with pytest.raises(TurnFailed):
            loop_with(store, Recorder("saved later")).run_turn(session, "hi", request_id=REQUEST)
        [thread] = started
        thread.join(timeout=10)
        assert not thread.is_alive()  # Stops once nothing waits.
        assert submissions.get(store, REQUEST)["status"] == "complete"
        assert contents(store, session) == [("user", "hi"), ("assistant", "saved later")]
        assert restart._settler is None


def test_a_database_that_is_down_keeps_the_answer_until_it_works(tmp_path, monkeypatch, pauses):
    def down():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(restart, "SETTLE_TRIES", 2)
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        turn = store.start_turn(session)
        store._connect = down  # Even holding the answer fails.
        with pytest.raises(restart.NotSaved):
            restart.save(store, turn, "kept in memory", {"model": "m", "provider": "p"})
        assert pauses == list(restart.RETRY_DELAYS)
        assert restart.settle() == 0 and restart.settle() == 0  # Past the tries, still down.
        assert restart.settle() == 0 and restart.waiting() == [turn]
        del store._connect
        assert held(store) == 0  # Never written: the answer lived only in Pi's memory.
        assert restart.settle() == 1
        assert store.get_turn(turn)["status"] == "complete"
        assert contents(store, session) == [("assistant", "kept in memory")]
        assert held(store) == 0


def test_pi_stopping_between_tries_leaves_the_answer_for_the_next_start(tmp_path, monkeypatch):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        faults = Faults(monkeypatch, 1)
        monkeypatch.setattr(restart, "_pause", lambda _delay: (_ for _ in ()).throw(Stop()))
        with pytest.raises(Stop):
            loop_with(store, Recorder("the answer")).run_turn(session, "hello", request_id=REQUEST)
        assert faults.tries == 1 and held(store) == 1
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "complete"
        assert contents(store, session) == [("user", "hello"), ("assistant", "the answer")]
        provider = Recorder()
        replay = loop_with(store, provider).run_turn(session, "hello", request_id=REQUEST)
        assert replay["message"]["content"] == "the answer" and provider.calls == []
        assert held(store) == 0


def test_pi_stopping_while_the_answer_waits_saves_it_once_at_the_next_start(tmp_path, monkeypatch):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        faults = Faults(monkeypatch, float("inf"))
        with pytest.raises(TurnFailed):
            loop_with(store, Recorder("the answer")).run_turn(session, "hello", request_id=REQUEST)
    faults.left = 0  # Pi stopped before the settle ran; the database works after the restart.
    with closing(restarted(path)) as store:
        assert submissions.get(store, REQUEST)["status"] == "complete"
        assert restart.settle() == 1  # The old wait finds its store gone and saves nothing.
        assert contents(store, session) == [("user", "hello"), ("assistant", "the answer")]


def test_a_stop_pressed_between_tries_ends_the_turn_and_drops_the_answer(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        faults, seen = Faults(monkeypatch, float("inf")), []

        def stop_pressed(delay):
            seen.append(delay)
            turn_control.cancel(store, running(store, session))

        monkeypatch.setattr(restart, "_pause", stop_pressed)
        with pytest.raises(TurnFailed, match="Owner stopped"):
            loop_with(store, Recorder("too late")).run_turn(session, "hello", request_id=REQUEST)
        assert seen == [0.25] and faults.tries == 1  # The next try was refused, not written.
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "cancelled" and receipt["final_message_id"] is None
        assert held(store) == 0 and restart.waiting() == []
        faults.left = 0
        again = loop_with(store, Recorder("fresh")).run_turn(
            session, "again", request_id="save_retry_request_0002"
        )
        assert again["message"]["content"] == "fresh"
        assert ("assistant", "too late") not in contents(store, session)


def test_a_stop_pressed_while_the_answer_waits_ends_the_turn_at_the_next_settle(
    tmp_path, monkeypatch
):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        faults = Faults(monkeypatch, float("inf"))
        with pytest.raises(TurnFailed):
            loop_with(store, Recorder("too late")).run_turn(session, "hello", request_id=REQUEST)
        turn_control.cancel(store, running(store, session))
        faults.left = 0
        assert restart.settle() == 1
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "cancelled" and receipt["final_message_id"] is None
        assert contents(store, session) == [("user", "hello")] and held(store) == 0


def test_a_steer_between_tries_answers_again_and_drops_the_stale_answer(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        faults, provider = Faults(monkeypatch, 1), Answers("stale", "revised")

        def steered(_delay):
            steer = steering.Steer(request_id="save_retry_steer_0001", text="Shorter, please.")
            steering.submit(store, running(store, session), steer)

        monkeypatch.setattr(restart, "_pause", steered)
        result = loop_with(store, provider).run_turn(session, "hello", request_id=REQUEST)
        assert result["message"]["content"] == "revised" and len(provider.calls) == 2
        assert faults.tries == 2  # The stale answer's second try was refused before writing.
        assert contents(store, session) == [
            ("user", "hello"),
            ("user", "Shorter, please."),
            ("assistant", "revised"),
        ]
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM steering_discarded_answers").fetchone()[0] == 1
        assert held(store) == 0 and restart.waiting() == []


def test_a_steer_that_reaches_a_waiting_answer_ends_the_turn_in_plain_words(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        faults = Faults(monkeypatch, float("inf"))
        with pytest.raises(TurnFailed):
            loop_with(store, Recorder("stale")).run_turn(session, "hello", request_id=REQUEST)
        turn = running(store, session)
        steering.active(
            store, turn, True
        )  # Its end was not written either, so steering stayed open.
        steering.submit(store, turn, steering.Steer(request_id="save_retry_steer_0002", text="No."))
        faults.left = 0
        assert restart.settle() == 1
        ended = store.get_turn(turn)
        assert ended["status"] == "failed" and ended["detail"] == restart.STEERED[False]
        assert ("assistant", "stale") not in contents(store, session) and held(store) == 0


def test_a_commit_that_landed_but_lost_its_reply_is_not_saved_twice(tmp_path, pauses):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        real, calls = store.complete_turn, []

        def landed_then_failed(*args, **kwargs):
            calls.append(1)
            message = real(*args, **kwargs)
            if len(calls) == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return message

        store.complete_turn = landed_then_failed
        result = loop_with(store, Recorder("once")).run_turn(session, "hello", request_id=REQUEST)
        assert result["message"]["content"] == "once" and len(calls) == 2 and pauses == [0.25]
        assert contents(store, session) == [("user", "hello"), ("assistant", "once")]
        assert submissions.get(store, REQUEST)["status"] == "complete"


def test_a_save_the_database_refuses_is_not_tried_again_at_once_and_ends_plainly(tmp_path, pauses):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        with store._connect() as db:
            db.executescript(
                "CREATE TRIGGER refuse_final BEFORE INSERT ON turn_messages "
                "WHEN NEW.purpose='final' BEGIN SELECT RAISE(ABORT,'refused'); END;"
            )
        loop = loop_with(store, Recorder("refused"))
        with pytest.raises(TurnFailed):
            loop.run_turn(session, "hello", request_id=REQUEST)
        assert pauses == []
        assert restart.settle() == 1  # Refused again: the turn ends, as the startup settle ends it.
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "failed"
        assert store.get_turn(receipt["turn_id"])["detail"] == restart.UNSAVED[False]
        assert held(store) == 0 and restart.waiting() == []
        with store._connect() as db:
            db.execute("DROP TRIGGER refuse_final")
        again = loop.run_turn(session, "again", request_id="save_retry_request_0002")
        assert again["message"]["content"] == "refused"  # The chat is free.


def test_an_acted_turn_whose_reply_keeps_failing_ends_waiting_for_the_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "SETTLE_TRIES", 3)
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        loop = loop_with(store, Recorder())
        turn = store.start_turn(session)
        store.mark_acted(turn)
        Faults(monkeypatch, float("inf"))
        with pytest.raises(ActedWithoutReply, match="could not save the answer yet"):
            loop._complete_turn(turn, "It ran.", acted=1, model="test-model", provider="recorder")
        assert restart.settle() == 0 and restart.settle() == 0
        assert store.get_turn(turn)["status"] == "running"
        assert restart.settle() == 1  # The database takes other writes: this save is a fault.
        ended = store.get_turn(turn)
        assert ended["status"] == "acted_no_reply" and ended["acted"]
        assert ended["detail"] == restart.UNSAVED[True] and held(store) == 0
