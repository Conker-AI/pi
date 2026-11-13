"""The migration ledger: each upgrade step runs once, is recorded, and a downgrade is refused."""

import sqlite3
from contextlib import closing

import pytest

from pi.conversation import forget_queue, forgetting, upgrades
from pi.conversation.store import Store
from pi.core import migrations
from pi.core.version import VERSION

IDS = [step.id for step in upgrades.MIGRATIONS]
TURN_COLUMNS = (
    "route_tier",
    "route_reason",
    "approval_request_id",
    "approval_tool_id",
    "approval_args",
    "approval_expires_at",
    "approval_intent",
    "acted",
    "usage_version",
)
OLD_TABLES = """
CREATE TABLE change_requests (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('ai_job','tool_draft','memory')),
 title TEXT NOT NULL, summary TEXT NOT NULL, payload TEXT NOT NULL,
 session_id TEXT REFERENCES sessions(id), turn_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('open','approved','declined','failed')),
 result TEXT, created_at REAL NOT NULL, decided_at REAL
);
CREATE TABLE keep_results (
 request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 kind TEXT NOT NULL CHECK(kind IN ('memory','ai_job')), target_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 undo TEXT CHECK(undo IN ('pending','done','gone','blocked')),
 revision INTEGER, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT ''
);
"""


def ledger(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT id, applied_at, pi_version FROM schema_migrations").fetchall()


def columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def old_database(path):
    """What a Pi from before the ledger and before these columns left: built by hand."""
    Store(path).close()
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "DROP TABLE schema_migrations;"
            "DROP TRIGGER new_turn_usage_version;"
            "DROP TABLE change_requests; DROP TABLE keep_results;"
            + OLD_TABLES
            + "INSERT INTO sessions(id,created_at) VALUES ('s1',1);"
            "INSERT INTO turns(id,session_id,status,started_at) VALUES ('t1','s1','complete',1);"
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES ('creq_old','s1','ai_job','job_1',1);"
        )
        for column in reversed(TURN_COLUMNS):
            db.execute(f"ALTER TABLE turns DROP COLUMN {column}")
        db.execute("ALTER TABLE attachments DROP COLUMN storage")
        db.execute("ALTER TABLE setup_model_probe_requests DROP COLUMN operation")
        db.commit()
        assert not columns(db, "turns") & set(TURN_COLUMNS)


def test_ids_are_append_only():
    # A database records these ids; renaming or reordering one breaks every older database.
    assert IDS == [
        "0001-turn-route-approval-usage",
        "0002-setup-probe-operation",
        "0003-attachment-storage",
        "0004-change-request-agent-team",
        "0005-keep-result-agent-team",
        "0006-notices",
    ]


def test_a_fresh_database_records_every_migration(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        assert store.schema_migration == IDS[-1]
    recorded = ledger(tmp_path / "pi.db")
    assert [row[0] for row in recorded] == IDS
    assert {row[2] for row in recorded} == {VERSION}


def test_an_old_database_upgrades_keeps_its_rows_and_records(tmp_path):
    path = tmp_path / "pi.db"
    old_database(path)
    with closing(Store(path)) as store:
        assert store.schema_migration == IDS[-1]
        with store._connect() as db:
            assert set(TURN_COLUMNS) <= columns(db, "turns")
            assert "storage" in columns(db, "attachments")
            assert "operation" in columns(db, "setup_model_probe_requests")
            assert db.execute("SELECT acted FROM turns WHERE id='t1'").fetchone()[0] == 0
            assert [r[0] for r in db.execute("SELECT target_id FROM keep_results")] == ["job_1"]
            indexes = {
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            assert {"change_requests_open", "keep_results_session", "keep_results_undo"} <= indexes
            db.execute(
                "INSERT INTO change_requests VALUES ('c2','team','t','s','{}',NULL,NULL,'open',"
                "NULL,1,NULL)"
            )
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        new = store.start_turn(store.create_session())
        assert store.get_turn(new)["usage_version"] == 1
    assert [row[0] for row in ledger(path)] == IDS


def test_starting_again_applies_nothing_new(tmp_path):
    path = tmp_path / "pi.db"
    Store(path).close()
    before = ledger(path)
    Store(path).close()
    assert ledger(path) == before
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        assert migrations.apply(db, upgrades.MIGRATIONS, VERSION) == []


def test_a_failing_migration_rolls_back_and_is_not_recorded(tmp_path):
    def create(db):
        db.execute("CREATE TABLE notes (id TEXT)")

    def broken(db):
        db.execute("ALTER TABLE notes ADD COLUMN body TEXT")
        db.execute("INSERT INTO notes VALUES ('n1','half')")
        raise RuntimeError("step failed")

    def fixed(db):
        db.execute("ALTER TABLE notes ADD COLUMN body TEXT")

    with closing(sqlite3.connect(tmp_path / "x.db", isolation_level=None)) as db:
        first = migrations.Migration("a", create)
        with pytest.raises(RuntimeError, match="step failed"):
            migrations.apply(db, [first, migrations.Migration("b", broken)], "test")
        assert [row[0] for row in db.execute("SELECT id FROM schema_migrations")] == ["a"]
        assert columns(db, "notes") == {"id"}
        assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
        assert migrations.apply(db, [first, migrations.Migration("b", fixed)], "test") == ["b"]
        assert migrations.last(db) == "b"


def test_a_migration_that_ends_its_own_transaction_is_refused(tmp_path):
    def commits(db):
        db.execute("CREATE TABLE notes (id TEXT)")
        db.commit()

    with closing(sqlite3.connect(tmp_path / "x.db", isolation_level=None)) as db:
        with pytest.raises(RuntimeError, match="ended its own transaction"):
            migrations.apply(db, [migrations.Migration("a", commits)], "test")
        assert migrations.last(db) is None


def test_a_database_from_a_newer_pi_refuses_to_start(tmp_path):
    path = tmp_path / "pi.db"
    Store(path).close()
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO schema_migrations VALUES ('0099-from-a-newer-pi', 1, '9.0.0')")
        db.commit()
    with pytest.raises(migrations.UnknownMigration, match="0099-from-a-newer-pi"):
        Store(path)
    assert len(ledger(path)) == len(IDS) + 1
    # The lease is released on failure: a second attempt refuses the same way.
    with pytest.raises(migrations.UnknownMigration):
        Store(path)


def newer(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO schema_migrations VALUES ('0099-from-a-newer-pi', 1, '9.0.0')")
        db.commit()


def test_forgetting_refuses_a_newer_pis_database_before_any_write(tmp_path):
    path = tmp_path / "pi.db"
    store = Store(path)
    session = store.create_session()
    store.close()
    newer(path)
    with pytest.raises(forgetting.ForgettingError, match="newer Pi"):
        forgetting.forget(path, session, "any")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT 1 FROM sessions WHERE id=?", (session,)).fetchone()


def test_the_startup_forget_queue_refuses_a_newer_pis_database(tmp_path):
    path = tmp_path / "pi.db"
    store = Store(path)
    session = store.create_session()
    store.close()
    assert forget_queue.hold(path)
    newer(path)
    forget_queue.queue(path, [{"sessionId": session, "sessionIds": [session]}])
    with pytest.raises(migrations.UnknownMigration, match="0099-from-a-newer-pi"):
        forget_queue.run_pending(path)
    # Nothing was touched: the hold stays, the chat stays queued and not forgotten.
    with closing(sqlite3.connect(path)) as db:
        triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert set(forget_queue.HOLDS) <= triggers
        assert db.execute("SELECT 1 FROM sessions WHERE id=?", (session,)).fetchone()
    assert forget_queue.pending_sessions(path) == {session}
