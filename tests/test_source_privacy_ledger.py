"""Shared source eligibility follows forgetting receipts across owner surfaces."""

import json
from contextlib import closing
from datetime import UTC, datetime

import pytest

from pi import agents, continuity, project_sources, session_settings, tasks, universal_search
from pi.store import Store

DAY = datetime(2026, 9, 21, 10, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "sources.db")) as value:
        yield value


def tombstone(store, sid, *, receipt_status="confirmed"):
    with store._connect() as db:
        db.execute(
            "INSERT INTO forgetting_receipts VALUES(?,?,0,?,0,0)",
            (f"del_{sid}", sid, receipt_status),
        )
        db.execute("INSERT INTO forgotten_sessions VALUES(?,?)", (sid, f"del_{sid}"))


def source(store, sid):
    with store._connect() as db:
        return session_settings.source_privacy(db, sid)


@pytest.mark.parametrize("status", ["open", "closed", "forked"])
@pytest.mark.parametrize("receipt_status", ["confirmed", "pending"])
def test_ledger_overrides_retained_status_settings_and_receipt_progress(
    store, status, receipt_status
):
    sid = store.create_session(title="Retained legacy title")
    if status != "open":
        store.close_session(sid, status)
    assert source(store, sid) == {
        "memoryDisabled": False,
        "harnessDisabled": False,
        "incognito": False,
    }
    tombstone(store, sid, receipt_status=receipt_status)
    assert store.get_session(sid)["status"] == status
    assert store.get_session(sid)["title"] == "Retained legacy title"
    assert source(store, sid) is None


def test_ledger_guard_precedes_invalid_legacy_settings_and_does_not_write(store):
    sid = store.create_session(title="Retained", privacy={"memoryDisabled": False})
    with store._connect() as db:
        db.execute(
            "UPDATE session_settings SET settings='invalid legacy JSON' WHERE session_id=?", (sid,)
        )
    tombstone(store, sid)
    with store._connect() as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        assert source(store, sid) is None
        assert monitor.execute("PRAGMA data_version").fetchone()[0] == version


@pytest.mark.parametrize("status", ["open", "closed", "forked"])
def test_nonforgotten_archived_sources_preserve_public_and_private_policy(store, status):
    public = store.create_session(title="Public source")
    private = store.create_session(title="Private source", privacy={"harnessDisabled": True})
    if status != "open":
        for sid in (public, private):
            store.close_session(sid, status)
    assert source(store, public) == {
        "memoryDisabled": False,
        "harnessDisabled": False,
        "incognito": False,
    }
    assert source(store, private) == {
        "memoryDisabled": False,
        "harnessDisabled": True,
        "incognito": True,
    }
    for sid in (public, private):
        resolved = project_sources.resolve(store, {"kind": "conversation", "sessionId": sid})
        assert resolved["originSessionId"] == sid and resolved["archived"] == (status != "open")
        assert resolved["privacy"] == source(store, sid)


def test_missing_and_status_only_forgotten_sources_still_fail_closed(store):
    assert source(store, "missing") is None
    sid = store.create_session(title="Old source")
    with store._connect() as db:
        db.execute("UPDATE sessions SET status='forgotten' WHERE id=?", (sid,))
    assert source(store, sid) is None


def test_ledger_hidden_activity_and_continuity_do_not_reveal_retained_origin(store):
    sid = store.create_session(title="Synthetic private retained title")
    turn = store.start_turn(sid)
    store.finish_turn(turn, "complete")
    records, coverage = universal_search.lexical(
        store, None, None, "run", {"sources": ["activity"]}, "metadata"
    )
    assert records and coverage == [{"source": "activity", "status": "searched"}]
    prior = continuity.briefing(store, now=DAY)
    assert len(prior["items"]) == 1 and prior["items"][0]["sessionId"] == sid
    identity = prior["items"][0]["eventId"]
    assert continuity.notifications(store, now=DAY)["items"]
    tombstone(store, sid)

    records, coverage = universal_search.lexical(
        store, None, None, "run", {"sources": ["activity"]}, "metadata"
    )
    assert records == [] and coverage == [{"source": "activity", "status": "searched"}]
    briefing = continuity.briefing(store, now=DAY)
    assert briefing["items"] == [] and briefing["summary"]["highlights"] == []
    assert briefing["summary"]["eventIds"] == []
    assert "Synthetic private" not in json.dumps(briefing)
    assert continuity.notifications(store, now=DAY)["items"] == []
    for acknowledge in (continuity.acknowledge, continuity.delivered):
        with pytest.raises(agents.AgentError) as error:
            acknowledge(store, continuity.Acknowledge(event_ids=[identity]))
        assert error.value.status == 404 and error.value.detail["code"] == "event_unavailable"
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM continuity_ack").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM continuity_delivered").fetchone()[0] == 0


def test_ledger_hides_conversation_and_task_project_sources_without_rewriting_them(store):
    sid = store.create_session(title="Retained project conversation")
    task = tasks.create(
        store,
        tasks.CreateTask(
            request_id="source_ledger_task",
            session_id=sid,
            outcome="Retained task outcome",
            criteria=["Review result"],
            run_ids=[],
        ),
    )
    refs = [{"kind": "conversation", "sessionId": sid}, {"kind": "task", "taskId": task["id"]}]
    for ref in refs:
        assert project_sources.resolve(store, ref)["originSessionId"] == sid
    tombstone(store, sid)
    for ref in refs:
        assert project_sources.resolve(store, ref) is None
    # This is a projection/privacy fix; it does not alter original retained data.
    assert store.get_session(sid)["title"] == "Retained project conversation"
    with store._connect() as db:
        assert (
            db.execute("SELECT outcome FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
            == "Retained task outcome"
        )
