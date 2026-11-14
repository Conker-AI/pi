import json
import sqlite3
import sys
from contextlib import closing
from uuid import NAMESPACE_URL, uuid5

import pytest

from pi.conversation.store import Store
from pi.memory import recovery


def outbox(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        message = store.append_message(store.create_session(), "user", "private")
    return path, message["id"]


def row(path, message_id):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return dict(
            db.execute("SELECT * FROM memory_outbox WHERE message_id=?", (message_id,)).fetchone()
        )


def change(path, message_id, **columns):
    sets = ",".join(f"{name}=?" for name in columns)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            f"UPDATE memory_outbox SET {sets} WHERE message_id=?", (*columns.values(), message_id)
        )
        db.commit()


def test_a_missing_database_is_refused_and_not_created(tmp_path):
    with pytest.raises(ValueError, match="Database not found"):
        recovery.repair(tmp_path / "absent.db", "msg_x")
    assert not (tmp_path / "absent.db").exists()


def test_a_message_without_an_outbox_record_is_refused(tmp_path):
    path, _message_id = outbox(tmp_path)
    with pytest.raises(ValueError, match="No outbox record"):
        recovery.repair(path, "msg_unknown")


@pytest.mark.parametrize("agent", ["", "   ", "a" * 101])
def test_an_empty_or_overlong_agent_id_changes_nothing(tmp_path, agent):
    path, message_id = outbox(tmp_path)
    before = row(path, message_id)
    with pytest.raises(ValueError, match="original nonempty agent ID"):
        recovery.repair(path, message_id, agent_id=agent)
    assert row(path, message_id) == before


def test_a_recorded_destination_cannot_be_retargeted(tmp_path):
    path, message_id = outbox(tmp_path)
    change(path, message_id, destination_agent_id="first")
    with pytest.raises(ValueError, match="already recorded"):
        recovery.repair(path, message_id, agent_id="second")
    assert row(path, message_id)["destination_agent_id"] == "first"
    recovery.repair(path, message_id, agent_id="first")


def test_an_agent_that_contradicts_the_saved_receipt_is_refused(tmp_path):
    path, message_id = outbox(tmp_path)
    saved = str(uuid5(NAMESPACE_URL, f"pi:original:{message_id}"))
    change(path, message_id, receipt=json.dumps({"id": saved}))
    with pytest.raises(ValueError, match="contradicts the saved receipt"):
        recovery.repair(path, message_id, agent_id="other")
    assert row(path, message_id)["destination_agent_id"] is None
    recovery.repair(path, message_id, agent_id="original")
    assert row(path, message_id)["destination_agent_id"] == "original"


def test_a_started_delivery_with_no_known_destination_needs_bind_origin_first(tmp_path):
    path, message_id = outbox(tmp_path)
    change(path, message_id, delivery_started=1, state="blocked", error="namespace unknown")
    with pytest.raises(ValueError, match="run bind-origin first"):
        recovery.repair(path, message_id)
    assert row(path, message_id)["state"] == "blocked"
    recovery.repair(path, message_id, agent_id="original")
    assert row(path, message_id)["state"] == "pending"


def test_retry_unblocks_only_a_blocked_record(tmp_path):
    path, message_id = outbox(tmp_path)
    change(path, message_id, state="blocked", error="refused", next_at=99)
    recovery.repair(path, message_id)
    fixed = row(path, message_id)
    assert (fixed["state"], fixed["error"], fixed["next_at"]) == ("pending", "", 0)
    change(path, message_id, state="done", error="kept")
    recovery.repair(path, message_id)
    assert row(path, message_id)["state"] == "done"


def run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["recovery", *argv])
    try:
        recovery.main()
    except SystemExit as exit_:
        return exit_.code, capsys.readouterr()
    return 0, capsys.readouterr()


def test_the_command_line_repairs_and_reports(tmp_path, monkeypatch, capsys):
    path, message_id = outbox(tmp_path)
    change(path, message_id, state="blocked")
    code, out = run(monkeypatch, capsys, "--db", str(path), "retry", "--message-id", message_id)
    assert code == 0 and "Delivery metadata repaired" in out.out
    assert row(path, message_id)["state"] == "pending"
    code, out = run(
        monkeypatch,
        capsys,
        "--db",
        str(path),
        "bind-origin",
        "--message-id",
        message_id,
        "--agent-id",
        "original",
    )
    assert code == 0 and row(path, message_id)["destination_agent_id"] == "original"


def test_the_command_line_exits_with_the_reason_on_a_refusal(tmp_path, monkeypatch, capsys):
    path, _message_id = outbox(tmp_path)
    code, out = run(monkeypatch, capsys, "--db", str(path), "retry", "--message-id", "msg_unknown")
    assert code == 1 and "No outbox record" in out.err
    code, out = run(
        monkeypatch, capsys, "--db", str(tmp_path / "absent.db"), "retry", "--message-id", "m"
    )
    assert code == 1 and "Database not found" in out.err
