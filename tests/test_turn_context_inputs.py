"""Actual answer history is inspectable without capturing later messages."""

import sqlite3
from contextlib import closing

import pytest
from test_loop import Recorder, loop_with
from test_tool_turns import CALL, FakeGate, build

from pi import context_controls, forgetting, turn_context
from pi.store import Store


def test_context_boundary_survives_new_messages_and_read_inspection(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        loop = loop_with(store, Recorder(), system_prompt="Original system instruction")
        result = loop.run_turn(sid, "Original request", request_id="context_capture_001")
        saved = turn_context.load(store, result["turn_id"])
        assert saved["message_ids"] == [result["submission"]["input_message_id"]]
        assert saved["prefix"][0]["content"] == "Original system instruction"
        store.append_message(sid, "user", "Later request")
        loop._history(sid, turn_id=result["turn_id"])
        assert turn_context.load(store, result["turn_id"]) == saved
        with store._connect() as db, pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE turn_context_inputs SET prefix='[]' WHERE turn_id=?", (result["turn_id"],)
            )
    with closing(Store(path)) as store:
        assert turn_context.load(store, result["turn_id"]) == saved


def test_final_tool_answer_snapshot_includes_receipt_but_not_its_answer(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        loop, _ = build(store, [CALL, "Final answer"], FakeGate())
        result = loop.run_turn(sid, "Use tool")
        saved = turn_context.load(store, result["turn_id"])
        rows = [store.get_message(identity) for identity in saved["message_ids"]]
        assert [row["role"] for row in rows] == ["user", "assistant", "tool"]
        assert result["message"]["id"] not in saved["message_ids"]


def test_forgetting_scrubs_non_message_prefix(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        result = loop_with(store, Recorder(), system_prompt="PRIVATE_PREFIX_MARKER_007").run_turn(
            sid, "Hi"
        )
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    assert b"PRIVATE_PREFIX_MARKER_007" not in path.read_bytes()
    with closing(Store(path)) as store, pytest.raises(context_controls.ContextError):
        turn_context.load(store, result["turn_id"])


def test_replay_uses_original_instructions_reply_and_boundary(tmp_path):
    from test_context_controls import policy

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        target = store.append_message(sid, "assistant", "Earlier explanation")
        original = policy({})
        original.sessionInstructions = "Original session instructions"
        context_controls.save(
            store, sid, context_controls.Update(expected_revision=0, policy=original)
        )
        provider = Recorder()
        loop = loop_with(store, provider, system_prompt="Original system")
        result = loop.run_turn(sid, "Explain again", reply_to=target["id"])
        changed = policy({target["id"]: "exclude"})
        changed.sessionInstructions = "Changed instructions"
        context_controls.save(
            store, sid, context_controls.Update(expected_revision=1, policy=changed)
        )
        store.append_message(sid, "user", "Later request must not be replayed")
        replay = turn_context.replay(store, result["turn_id"])
        assert replay["messages"] == provider.calls[0]
        assert replay["context"]["policy"] == original.model_dump()
        assert len(provider.calls) == 1


def test_replay_keeps_recorded_tool_results_without_dispatch(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        gate = FakeGate()
        loop, provider = build(store, [CALL, "Final answer"], gate)
        result = loop.run_turn(sid, "Use tool")
        assert turn_context.replay(store, result["turn_id"])["messages"] == provider.sent[-1]
        assert len(gate.invocations) == 1 and len(provider.sent) == 2


def test_replay_preserves_attachment_passages_and_refuses_forgotten_source(tmp_path):
    from test_attachment_turns import upload

    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        identity = upload(store, sid)
        provider = Recorder()
        result = loop_with(store, provider).run_turn(
            sid, "Read source", request_id="replay_attachment_001", attachment_ids=[identity]
        )
        assert turn_context.replay(store, result["turn_id"])["messages"] == provider.calls[0]
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    with closing(Store(path)) as store:
        with pytest.raises(context_controls.ContextError):
            turn_context.replay(store, result["turn_id"])
        assert len(provider.calls) == 1


@pytest.mark.parametrize("flag", ["memoryDisabled", "harnessDisabled"])
def test_replay_cannot_bypass_new_stricter_privacy(tmp_path, flag):
    from pi import session_settings

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        result = loop_with(store, Recorder()).run_turn(sid, "Public request")
        settings = session_settings.Settings.model_validate(session_settings.DEFAULT)
        setattr(settings.privacy, flag, True)
        session_settings.save(
            store, sid, session_settings.Update(expected_revision=0, settings=settings)
        )
        with pytest.raises(context_controls.ContextError) as failure:
            turn_context.replay(store, result["turn_id"])
        assert failure.value.detail["code"] == "retry_privacy_changed"


def test_legacy_or_unfinished_turn_cannot_invent_a_replay(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        identity = store.start_turn(sid)
        with pytest.raises(context_controls.ContextError):
            turn_context.replay(store, identity)
        store.finish_turn(identity, "complete")
        with pytest.raises(context_controls.ContextError) as failure:
            turn_context.replay(store, identity)
        assert failure.value.detail["code"] == "context_unavailable"
