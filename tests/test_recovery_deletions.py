import sqlite3
from contextlib import closing

import pytest

from pi import forgetting
from pi import recovery_deletions as recovery
from pi.access import MaintenanceRequired
from pi.store import Store


def fixture(tmp_path):
    source, target = tmp_path / "current.db", tmp_path / "restored.db"
    with closing(Store(source)) as store:
        root = store.create_session(title="erase this")
        child = store.create_session(parent_id=root, title="also erase")
        kept = store.create_session(title="keep this")
        for identity in (root, child):
            store.append_message(identity, "user", "unique secret to erase")
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    forgetting.forget(source, root, forgetting.preview(source, root)["confirmation"])
    return source, target, root, child, kept


def test_replay_removes_old_backup_content_and_retains_startup_hold(tmp_path):
    source, target, root, child, kept = fixture(tmp_path)
    plan = recovery.preview(source, target)
    result = recovery.replay(source, target, plan["confirmation"])
    assert result["appliedSessionIds"] == sorted([root, child])
    assert result["recoveryHeld"] and not result["promotesRecovery"]
    assert b"unique secret to erase" not in target.read_bytes()
    with closing(sqlite3.connect(target)) as db:
        assert (
            db.execute("SELECT title FROM sessions WHERE id=?", (kept,)).fetchone()[0]
            == "keep this"
        )
        assert db.execute("SELECT count(*) FROM forgotten_sessions").fetchone()[0] == 2
    with pytest.raises(MaintenanceRequired, match="held"):
        Store(target)
    assert recovery.replay(source, target, plan["confirmation"])["appliedSessionIds"] == []


def test_stale_preview_and_running_target_refuse_before_changes(tmp_path):
    source, target, root, _child, kept = fixture(tmp_path)
    plan = recovery.preview(source, target)
    with closing(Store(target)), pytest.raises(MaintenanceRequired):
        recovery.replay(source, target, plan["confirmation"])
    forgetting.forget(source, kept, forgetting.preview(source, kept)["confirmation"])
    with pytest.raises(forgetting.ForgettingError, match="scope changed"):
        recovery.replay(source, target, plan["confirmation"])
    with closing(Store(target)) as store:
        assert store.get_session(root)["title"] == "erase this"


def test_failure_keeps_hold_and_retry_finishes_cleanup(tmp_path, monkeypatch):
    source, target, *_ = fixture(tmp_path)
    plan = recovery.preview(source, target)
    scrub = forgetting._scrub
    monkeypatch.setattr(forgetting, "_scrub", lambda db: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        recovery.replay(source, target, plan["confirmation"])
    with pytest.raises(MaintenanceRequired):
        Store(target)
    monkeypatch.setattr(forgetting, "_scrub", scrub)
    assert recovery.replay(source, target, plan["confirmation"])["recoveryHeld"]


def test_unrelated_source_cannot_certify_recovery(tmp_path):
    _source, target, *_ = fixture(tmp_path)
    unrelated = tmp_path / "other.db"
    with closing(Store(unrelated)) as store:
        store.create_session()
    with pytest.raises(forgetting.ForgettingError, match="does not cover"):
        recovery.preview(unrelated, target)
