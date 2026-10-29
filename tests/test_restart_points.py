"""Pi stopping at each point of a turn, with the real store on disk and a fresh start after.

Each test stops the process at one point (a BaseException passes every handler, as a killed
process would), opens a new Store and Loop on the same file, runs the startup recovery, and reads
the receipt by request id the way the dashboard does: the outcome is true, nothing runs twice, and
an answer that was produced is kept.
"""

import sqlite3
from contextlib import closing

import pytest
from test_loop import Recorder, loop_with
from test_tool_turns import CALL, FakeGate, Scripted, build

from pi.actions import identities as actions
from pi.actions.toolgate import ToolResult
from pi.agents import change_requests
from pi.conversation import forgetting, submissions
from pi.conversation.store import Store
from pi.models.routing import Router
from pi.turns import control as turn_control
from pi.turns import restart
from pi.turns.loop import Loop

REQUEST = "restart_request_0001"
REMEMBER = '{"tool": "conker.remember", "args": {"text": "The exam is on Thursday."}}'


class Stop(BaseException):
    """The process dying: no handler runs."""


def stop(*_args, **_kwargs):
    raise Stop()


def restarted(path):
    store = Store(path)
    store.mark_interrupted_turns()  # What Pi's startup runs before taking requests.
    return store


def held(store):
    with store._connect() as db:
        return db.execute("SELECT COUNT(*) FROM held_answers").fetchone()[0]


def observations(store, session):
    return [m["content"] for m in store.messages(session) if m["role"] == "tool"]


def test_stop_after_admission_keeps_the_text_and_calls_no_model(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        loop = loop_with(store, Recorder())
        loop._history = stop  # Admitted, still preparing: no turn yet.
        with pytest.raises(Stop):
            loop.run_turn(session, "keep me", request_id=REQUEST)
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "preparation_interrupted"
        assert receipt["pending_text"] == "keep me" and store.turns(session) == []
        provider = Recorder()
        replay = loop_with(store, provider).run_turn(session, "keep me", request_id=REQUEST)
        assert replay["replayed"] and provider.calls == []


def test_stop_during_the_model_call_says_so_plainly_and_frees_the_chat(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        provider = Recorder()
        provider.complete = stop
        with pytest.raises(Stop):
            loop_with(store, provider).run_turn(session, "hello", request_id=REQUEST)
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "interrupted" and not receipt["acted"]
        assert receipt["final_message_id"] is None
        assert store.get_turn(receipt["turn_id"])["detail"] == (
            "Pi restarted before the answer was finished. Your message is kept; "
            "send it again to retry."
        )
        provider = Recorder("second try")
        loop = loop_with(store, provider)
        assert loop.run_turn(session, "hello", request_id=REQUEST)["replayed"]
        assert provider.calls == []  # Same request id: read, never run again.
        again = loop.run_turn(session, "hello", request_id="restart_request_0002")
        assert again["message"]["content"] == "second try" and len(provider.calls) == 1


def test_stop_before_the_answer_is_saved_keeps_that_answer(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        store.complete_turn = stop  # Dies inside the final commit.
        with pytest.raises(Stop):
            loop_with(store, Recorder("the answer")).run_turn(session, "hello", request_id=REQUEST)
        assert submissions.get(store, REQUEST)["final_message_id"] is None
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "complete" and receipt["final_message_id"]
        assert store.get_message(receipt["final_message_id"])["content"] == "the answer"
        turn = store.get_turn(receipt["turn_id"])
        assert turn["model"] == "test-model" and turn["input_tokens"] == 11
        assert [ref["purpose"] for ref in receipt["message_refs"]] == ["input", "final"]
        provider = Recorder()
        replay = loop_with(store, provider).run_turn(session, "hello", request_id=REQUEST)
        assert replay["message"]["content"] == "the answer" and provider.calls == []
        assert held(store) == 0


def test_an_answer_whose_commit_failed_is_saved_at_the_next_start(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        with store._connect() as db:
            db.executescript(
                "CREATE TRIGGER fail_final BEFORE UPDATE OF status ON turns "
                "WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT,'disk trouble'); END;"
            )
        with pytest.raises(sqlite3.IntegrityError, match="disk trouble"):
            loop_with(store, Recorder("kept")).run_turn(session, "hello", request_id=REQUEST)
        with store._connect() as db:
            db.execute("DROP TRIGGER fail_final")  # The trouble passes; Pi restarts.
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "complete"
        assert store.get_message(receipt["final_message_id"])["content"] == "kept"


def test_a_held_answer_the_owner_stopped_is_not_saved(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()

        def stopped_then_died(turn_id, *_args, **_kwargs):
            turn_control.cancel(store, turn_id)  # Stop pressed as the answer arrived.
            raise Stop()

        store.complete_turn = stopped_then_died
        with pytest.raises(Stop):
            loop_with(store, Recorder("too late")).run_turn(session, "hello", request_id=REQUEST)
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "cancelled" and receipt["final_message_id"] is None
        assert [m["role"] for m in store.messages(session)] == ["user"] and held(store) == 0


def test_a_steered_answer_is_dropped_so_a_restart_cannot_save_it(tmp_path):
    from pi.turns import steering

    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        loop = loop_with(store, Recorder())
        turn = store.start_turn(session)
        store.complete_turn = lambda *_args, **_kwargs: (_ for _ in ()).throw(steering.Pending())
        with pytest.raises(steering.Pending):
            loop._complete_turn(turn, "stale", model="test-model", provider="recorder")
        assert held(store) == 0


def test_stop_after_saving_leaves_the_answer_on_the_receipt(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        loop = loop_with(store, Recorder("saved"))
        loop.memory.status = stop  # Saved; dies before the browser hears back.
        with pytest.raises(Stop):
            loop.run_turn(session, "hello", request_id=REQUEST)
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "complete"
        assert store.get_message(receipt["final_message_id"])["content"] == "saved"
        provider = Recorder()
        replay = loop_with(store, provider).run_turn(session, "hello", request_id=REQUEST)
        assert replay["message"]["content"] == "saved" and provider.calls == []


def test_stop_during_a_toolgate_action_checks_it_and_never_dispatches_it_again(tmp_path):
    class Gate(FakeGate):
        def __init__(self):
            super().__init__()
            self.checked = []

        def invoke(self, *args, **kwargs):
            super().invoke(*args, **kwargs)
            raise Stop()  # Dispatched; no receipt reached Pi.

        def check_action(self, action_id, tool_id):
            self.checked.append(action_id)
            return ToolResult(ok=True, result={"echoed": {"text": "hi"}}, tool_id=tool_id)

    path, gate = tmp_path / "pi.db", Gate()
    with closing(Store(path)) as store:
        session = store.create_session()
        loop, _ = build(store, [CALL], gate)
        with pytest.raises(Stop):
            loop.run_turn(session, "send", request_id=REQUEST)
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "outcome_unknown"
        assert "it will not run twice" in store.get_turn(receipt["turn_id"])["detail"]
        loop, _ = build(store, ["It said hi."], gate)
        assert loop.run_turn(session, "send", request_id=REQUEST)["status"] == "outcome_unknown"
        result = loop.resume_turn(receipt["turn_id"])
        assert result["message"]["content"] == "It said hi."
        assert len(gate.invocations) == 1 and gate.checked == [
            actions.latest(store, result["turn_id"])["id"]
        ]


def test_stop_after_the_action_was_recorded_asks_only_for_the_reply(tmp_path):
    class StopsNarrating(Scripted):
        def complete(self, messages, *, model):
            if self.sent:
                raise Stop()
            return super().complete(messages, model=model)

    path, gate = tmp_path / "pi.db", FakeGate()
    with closing(Store(path)) as store:
        session = store.create_session()
        router = Router(local_provider=StopsNarrating([CALL]), local_model="test-model")
        with pytest.raises(Stop):
            Loop(store, router, toolgate=gate).run_turn(session, "send", request_id=REQUEST)
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "acted_no_reply" and receipt["acted"]
        loop, _ = build(store, ["It ran."], gate)
        assert loop.resume_turn(receipt["turn_id"])["message"]["content"] == "It ran."
        assert len(gate.invocations) == 1


def test_stop_after_conkers_own_tool_filed_waits_only_for_the_reply(tmp_path, monkeypatch):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        loop, _ = build(store, [REMEMBER], FakeGate())
        monkeypatch.setattr(actions, "record", stop)  # Filed in the Inbox; result not recorded.
        with pytest.raises(Stop):
            loop.run_turn(session, "Remember the exam.", request_id=REQUEST)
        monkeypatch.undo()
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "acted_no_reply" and receipt["acted"]
        [request] = change_requests.listing(store)
        assert request["id"] in observations(store, session)[0]
        gate = FakeGate()
        loop, _ = build(store, ["It is waiting in your Inbox."], gate)
        result = loop.resume_turn(receipt["turn_id"])
        assert result["message"]["content"] == "It is waiting in your Inbox."
        assert len(change_requests.listing(store)) == 1 and gate.invocations == []
        final = submissions.get(store, REQUEST)
        assert [ref["purpose"] for ref in final["message_refs"]] == [
            "input",
            "intermediate",
            "tool_result",
            "final",
        ]


def test_stop_before_conkers_own_tool_filed_anything_says_nothing_was_done(tmp_path, monkeypatch):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        loop, _ = build(store, [REMEMBER], FakeGate())
        monkeypatch.setattr(change_requests, "file", stop)
        with pytest.raises(Stop):
            loop.run_turn(session, "Remember the exam.", request_id=REQUEST)
        monkeypatch.undo()
    with closing(restarted(path)) as store:
        receipt = submissions.get(store, REQUEST)
        assert receipt["status"] == "interrupted" and not receipt["acted"]
        assert '"not_done"' in observations(store, session)[0]
        assert change_requests.listing(store) == []
        loop, _ = build(store, ["Done now."], FakeGate())
        again = loop.run_turn(session, "Try again.", request_id="restart_request_0002")
        assert again["message"]["content"] == "Done now."  # The chat is free.


def test_forgetting_a_chat_removes_its_held_answer(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        restart.hold(store, store.start_turn(session), "held-answer-to-forget", {})
    plan = forgetting.preview(path, session)
    forgetting.forget(path, session, plan["confirmation"])
    with closing(Store(path)) as store:
        assert held(store) == 0
    for entry in path.parent.glob("pi.db*"):
        assert b"held-answer-to-forget" not in entry.read_bytes()
