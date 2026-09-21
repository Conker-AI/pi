"""Scheduled outcomes join continuity without fictional conversation/run ownership."""

import json
import sqlite3
from contextlib import closing

import pytest
from test_continuity import event
from test_jobs import definition

from pi import continuity, jobs
from pi.store import Store


def claim(store, request="continuity_job_001"):
    job = jobs.create(store, definition(), now=0)
    return job, jobs.run_now(store, job["id"], request, now=100)


def test_mixed_feed_paging_and_durable_job_acknowledgement(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        event(store)
        job, run = claim(store)
        jobs.dispatch_claim(store, run, lambda *a, **kw: {"status": "completed"})
        newest = continuity.briefing(store, limit=1)
        item = newest["items"][0]
        assert item["kind"] == "job_status"
        assert item["jobId"] == job["id"] and item["scheduledRunId"] == run["id"]
        assert item["sessionId"] is None and item["runId"] is None
        assert not item["needsAttention"]
        older = continuity.briefing(store, before=newest["nextCursor"])
        assert [i["kind"] for i in older["items"]] == ["run_status"]
        continuity.acknowledge(store, continuity.Acknowledge(event_ids=[item["eventId"]]))
    with closing(Store(path)) as store:
        assert [i["kind"] for i in continuity.briefing(store)["items"]] == ["run_status"]


def test_reconciliation_supersedes_unknown_without_repeating_effect(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        _, run = claim(store)
        invocations = []

        def uncertain(*args, **kwargs):
            invocations.append(kwargs)
            raise TimeoutError()

        jobs.dispatch_claim(store, run, uncertain)
        before = continuity.briefing(store)["items"][0]
        assert before["status"] == "outcome_unknown" and before["needsAttention"]
        continuity.acknowledge(store, continuity.Acknowledge(event_ids=[before["eventId"]]))

        class Receipt:
            def reconcile(self, *args, **kwargs):
                return {"status": "completed", "secret_result": "not in feed"}

        jobs.reconcile(store, run["id"], Receipt())
        after = continuity.briefing(store)["items"][0]
        assert after["status"] == "completed" and after["eventId"] != before["eventId"]
        assert len(invocations) == 1
        assert "secret_result" not in json.dumps(after)


def test_job_status_events_are_atomic_and_immutable(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        _, run = claim(store)
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE scheduled_runs SET status='failed' WHERE id=?", (run["id"],))
            db.rollback()
        assert continuity.briefing(store)["items"] == []
        jobs.dispatch_claim(store, run, lambda *a, **kw: {"status": "failed"})
        with store._connect() as db, pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE continuity_order SET to_status='completed'")
        assert continuity.briefing(store)["items"][0]["status"] == "failed"


def test_upgrade_preserves_seen_activity_and_observes_old_job_state(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        # Reconstruct the pre-index schema boundary, without touching runtime evidence.
        with store._connect() as db:
            db.execute("DROP TRIGGER continuity_activity")
            db.execute("DROP TRIGGER continuity_job_status")
            db.execute("DROP TABLE continuity_ack")
            db.execute("DROP TABLE continuity_order")
            db.execute("DROP TABLE continuity_migrations")
        event(store)
        with store._connect() as db:
            identity = db.execute(
                "SELECT id FROM activity_events WHERE kind='run_status'"
            ).fetchone()[0]
            db.execute("INSERT INTO continuity_seen VALUES (?,1)", (identity,))
        _, run = claim(store)
        jobs.dispatch_claim(store, run, lambda *a, **kw: {"status": "completed"})
    with closing(Store(path)) as store:
        items = continuity.briefing(store)["items"]
        assert len(items) == 1 and items[0]["scheduledRunId"] == run["id"]
        assert items[0]["provenance"] == "state-observed"
        continuity.acknowledge(store, continuity.Acknowledge(event_ids=[items[0]["eventId"]]))
    with closing(Store(path)) as store:
        assert continuity.briefing(store)["items"] == []
