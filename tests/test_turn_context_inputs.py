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
