"""One notices table for plan alerts and AI job notes; migration 0006 moved the old rows."""

import sqlite3
from contextlib import closing

import pytest

from pi.conversation.store import Store
from pi.owner import notices, plan_alerts
from pi.schedule import agent_jobs

OLD = """
CREATE TABLE plan_alerts (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, window_id TEXT NOT NULL, label TEXT NOT NULL,
    threshold INTEGER NOT NULL, used_percent INTEGER NOT NULL, resets_at TEXT,
    raised_at REAL NOT NULL, reset_at REAL, dismissed_at REAL
);
CREATE UNIQUE INDEX plan_alerts_one_open ON plan_alerts(provider, window_id) WHERE reset_at IS NULL;
CREATE TABLE agent_job_notes (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL,
 session_id TEXT NOT NULL, message_id TEXT, title TEXT NOT NULL, detail TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','dismissed')),
 created_at REAL NOT NULL, dismissed_at REAL
);
"""
OPEN = "plan_" + "a" * 32
RESET = "plan_" + "b" * 32
NOTE = "note_" + "c" * 32
GONE = "note_" + "d" * 32


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def older_pi(path):
    """A database as a Pi before 0006 left it: the two old tables with rows, 0006 not run."""
    with closing(Store(path)) as store:
        session = store.create_session(title="Job chat")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "DELETE FROM schema_migrations WHERE id='0006-notices';"
            + OLD
            + f"""
            INSERT INTO plan_alerts VALUES
             ('{OPEN}','chatgpt','five_hour','5-hour',80,93,'2026-11-13T10:00:00Z',5,NULL,NULL),
             ('{RESET}','claude','weekly','weekly',80,85,NULL,2,4,3);
            INSERT INTO agent_job_notes VALUES
             ('{NOTE}','run_1','job_1','{session}','msg_1','Bill due','Friday','open',7,NULL),
             ('{GONE}','run_2','job_1','{session}',NULL,'Old','','dismissed',6,NULL);
            """
        )
    return session


def test_0006_moves_both_tables_keeps_ids_and_shapes(tmp_path):
    path = tmp_path / "pi.db"
    session = older_pi(path)
    with closing(Store(path)) as store:
        assert store.schema_migration == "0006-notices"
        with store._connect() as db:
            assert not {"plan_alerts", "agent_job_notes"} & tables(db)
        assert plan_alerts.needs_you(store) == [
            {
                "kind": "plan",
                "id": OPEN,
                "title": "ChatGPT 5-hour limit 93% used",
                "since": 5,
                "usedPercent": 93,
                "resetsAt": "2026-11-13T10:00:00Z",
                "source": {"kind": "plan-alert", "alertId": OPEN},
            }
        ]
        assert [r["title"] for r in plan_alerts.resets(store, 0)] == ["Claude weekly limit reset"]
        assert agent_jobs.notes(store) == [
            {
                "id": NOTE,
                "jobId": "job_1",
                "sessionId": session,
                "messageId": "msg_1",
                "title": "Bill due",
                "detail": "Friday",
                "createdAt": 7,
            }
        ]
        # A dismissed note stays dismissed; the old ids still dismiss.
        assert agent_jobs.dismiss(store, GONE)["changed"] is False
        assert plan_alerts.dismiss(store, OPEN) == {"id": OPEN, "dismissed": True}
        assert agent_jobs.dismiss(store, NOTE)["changed"] is True
        assert plan_alerts.needs_you(store) == [] and agent_jobs.notes(store) == []
    # A second start recreates neither old table.
    with closing(Store(path)) as store, store._connect() as db:
        assert not {"plan_alerts", "agent_job_notes"} & tables(db)


def test_an_open_plan_alert_still_counts_after_the_move(tmp_path):
    path = tmp_path / "pi.db"
    older_pi(path)
    with closing(Store(path)) as store:
        reading = {
            "provider": "chatgpt",
            "available": True,
            "windows": [
                {
                    "id": "five_hour",
                    "label": "5-hour",
                    "usedPercent": 97,
                    "resetsAt": "2026-11-13T10:00:00Z",
                }
            ],
        }
        # Same period: no second alert, and the peak goes up with the title.
        assert plan_alerts.record(store, reading, 80, now=10) == []
        [item] = plan_alerts.needs_you(store)
        assert item["id"] == OPEN and item["usedPercent"] == 97
        assert item["title"] == "ChatGPT 5-hour limit 97% used"


def test_one_note_per_run_and_only_known_kinds(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store, store._connect() as db:
        common = {"kind": notices.AGENT_JOB, "subject": "run_1", "title": "t", "facts": {}}
        assert notices.insert(db, identity="note_1", created_at=1, **common)
        assert not notices.insert(db, identity="note_2", created_at=2, **common)
        with pytest.raises(ValueError):
            notices.insert(
                db, identity="x", kind="mystery", subject="s", title="t", created_at=1, facts={}
            )


def test_forgetting_a_chat_takes_its_notes_and_leaves_plan_alerts(tmp_path):
    path = tmp_path / "pi.db"
    session = older_pi(path)
    with closing(Store(path)) as store, store._connect() as db:
        agent_jobs.redact(db, [session])
        db.commit()
        kinds = [r[0] for r in db.execute("SELECT kind FROM notices ORDER BY kind")]
    assert kinds == ["plan", "plan"]


def test_a_note_left_by_an_older_pi_after_its_chat_was_forgotten_goes_at_start(tmp_path):
    # An older Pi (a code rollback without the snapshot) forgot the chat but could not see
    # notices, so the note's text stayed there. The next start removes it.
    path = tmp_path / "pi.db"
    session = older_pi(path)
    with closing(Store(path)):
        pass
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO forgotten_sessions VALUES (?, 'rcpt_test')", (session,))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM notices WHERE session_id=?", (session,)).fetchone()[
            0
        ]
    with closing(Store(path)), closing(sqlite3.connect(path)) as db:
        assert not db.execute("SELECT 1 FROM notices WHERE session_id=?", (session,)).fetchone()
        assert db.execute("SELECT COUNT(*) FROM notices WHERE kind='plan'").fetchone()[0] == 2
