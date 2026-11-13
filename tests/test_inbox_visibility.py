"""The Inbox names jobs and suggestions: none from a forgotten or trashed chat, none turned off."""

import json
from datetime import UTC, datetime

import pytest

from pi.agents import inbox
from pi.conversation import archive
from pi.conversation.store import Store
from pi.schedule import agent_jobs, job_models, jobs

HOUR = 3600
# 15:00 in the default zone: outside quiet hours, so repeating AI jobs run.
DAY = datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def job(store, name, at, created_by="conker"):
    body = job_models.AgentJobCreate.model_validate(
        dict(
            name=name,
            instructions="Look around.",
            timing=dict(kind="interval", time="00:00", day=0, hours=2),
            timeZone="UTC",
        )
    )
    return agent_jobs.create(store, body, now=at, created_by=created_by)["id"]


def revision(store, job_id):
    with store._connect() as db:
        return db.execute("SELECT revision FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()[0]


def kept_from(store, session, job_id):
    with store._connect() as db:
        db.execute(
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES (?,?,?,?,0)",
            (f"req_{job_id}", session, "ai_job", job_id),
        )
        db.commit()


def finish_runs(store, now):
    claimed = jobs.claim_due(store, now=now)
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='complete'")
        db.commit()
    return claimed


def shown(store, now):
    view = inbox.inbox(store, now=now)
    return json.dumps(view["upcoming"]), json.dumps(view["handled"])


def test_a_job_kept_from_a_trashed_chat_leaves_upcoming_and_handled(store):
    now = DAY
    chat = store.create_session(title="Plans")
    kept = job(store, "zq kept job", now - 3 * HOUR)
    adopted = job(store, "zq adopted job", now - 3 * HOUR)
    job(store, "zq own job", now - 3 * HOUR, created_by="owner")
    for target in (kept, adopted):
        kept_from(store, chat, target)
    agent_jobs.adopt(
        store, adopted, job_models.Adopt(expected_revision=revision(store, adopted)), now=now - HOUR
    )
    assert len(finish_runs(store, now)) == 3
    upcoming, handled = shown(store, now)
    for text in (upcoming, handled):
        assert "zq kept" in text and "zq adopted" in text and "zq own" in text

    archive.trash(store, archive.Trash(sessions=[chat], trashed=True))
    upcoming, handled = shown(store, now)
    assert "zq kept" not in upcoming and "zq kept" not in handled
    # Adopted, it is his; his own job never came from that chat.
    for text in (upcoming, handled):
        assert "zq adopted" in text and "zq own" in text

    archive.trash(store, archive.Trash(sessions=[chat], trashed=False))
    upcoming, handled = shown(store, now)
    assert "zq kept" in upcoming and "zq kept" in handled


def test_a_turned_off_job_is_not_upcoming(store):
    now = DAY
    job(store, "zq on", now, created_by="owner")
    off = job(store, "zq off", now, created_by="owner")

    def turn(enabled):
        change = job_models.StateChange(expected_revision=revision(store, off), enabled=enabled)
        jobs.set_enabled(store, off, change, now=now)

    turn(False)
    upcoming, _ = shown(store, now)
    assert "zq on" in upcoming and "zq off" not in upcoming
    turn(True)
    assert "zq off" in shown(store, now)[0]
