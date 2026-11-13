"""Chats made from a hidden chat: what stays visible, and what was already hidden at a time.

The chat of an AI job Conker kept from a hidden chat is hidden with it only while the job is
still Conker's; once the owner adopted the job it is listed and runs as his, so its chat and its
Inbox notes stay visible (pi.conversation.visibility, pi.schedule.jobs.ADOPTED). `hidden_by`
names the chats that were already hidden at a given time, a fork of a trashed chat included, so
a reader that left them out then (the proposal pass) can tell what it never read.
"""

import types
from contextlib import closing
from datetime import UTC, datetime

import pytest

from pi.context import universal_search as search
from pi.conversation import archive, visibility
from pi.conversation.store import Store
from pi.schedule import agent_jobs, job_models, jobs

MARK = "zqdescend-7713"
DAY = datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, sid, trashed=True):
    archive.trash(store, archive.Trash(sessions=[sid], trashed=trashed))


def hidden_now(store, ids):
    with store._connect() as db:
        return visibility.hidden(db, ids)


def kept_job(store, session_id, name):
    """An AI job Conker made and kept from `session_id`; returns the job id and its chat."""
    body = job_models.AgentJobCreate.model_validate(
        dict(
            name=name,
            instructions="Look around.",
            timing=dict(kind="interval", time="00:00", day=0, hours=2),
            timeZone="Asia/Jerusalem",
        )
    )
    job = agent_jobs.create(store, body, now=DAY, created_by="conker")["id"]
    with store._connect() as db:
        db.execute(
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES (?,?,'ai_job',?,0)",
            (f"req_{job}", session_id, job),
        )
        db.commit()
    return job, agent_jobs.session_for(store, job)


def adopt(store, job):
    revision = jobs.get_job(store, job)["revision"]
    agent_jobs.adopt(store, job, job_models.Adopt(expected_revision=revision), now=DAY)


def note(store, job, chat):
    agent_jobs._note(store, f"run_{job}", job, chat, None, f"{MARK} note", "detail", DAY)


def test_the_chat_of_an_adopted_job_stays_visible_when_its_source_is_trashed(store):
    source = store.create_session(title=f"{MARK} source")
    mine, mine_chat = kept_job(store, source, f"{MARK} adopted")
    conker, conker_chat = kept_job(store, source, f"{MARK} still conker's")
    adopt(store, mine)
    note(store, mine, mine_chat)
    note(store, conker, conker_chat)
    trash(store, source)
    # The adopted job is listed and runs as the owner's, so its chat and notes show too.
    assert mine in {row["id"] for row in jobs.list_jobs(store)}
    assert hidden_now(store, [mine_chat, conker_chat]) == {conker_chat}
    assert [item["sessionId"] for item in agent_jobs.notes(store)] == [mine_chat]
    listed = {row["id"] for row in store.list_sessions()}
    assert mine_chat in listed and conker_chat not in listed
    found = {row["recordId"] for row in search.conversation_metadata(store, MARK)[0]}
    assert found == {mine_chat}
    trash(store, source, trashed=False)
    assert hidden_now(store, [mine_chat, conker_chat]) == set()
    assert {item["sessionId"] for item in agent_jobs.notes(store)} == {mine_chat, conker_chat}


def test_a_job_adopted_after_its_source_was_trashed_shows_again(store):
    source = store.create_session(title=f"{MARK} source")
    job, chat = kept_job(store, source, f"{MARK} job")
    trash(store, source)
    assert hidden_now(store, [chat]) == {chat}
    adopt(store, job)
    assert hidden_now(store, [chat]) == set()


def already_hidden(store, at, ids):
    """The chats among `ids` that were already hidden at the time `at`."""
    with store._connect() as db:
        db.execute("CREATE TEMP TABLE IF NOT EXISTS probe(at REAL)")
        db.execute("DELETE FROM probe")
        db.execute("INSERT INTO probe VALUES (?)", (at,))
        query = (
            f"SELECT s.id FROM sessions s, probe WHERE {visibility.hidden_by('s.id', 'probe.at')}"
        )
        return {row[0] for row in db.execute(query)} & set(ids)


def trash_at(monkeypatch, store, sid, at):
    with monkeypatch.context() as patch:
        patch.setattr(archive, "time", types.SimpleNamespace(time=lambda: at))
        trash(store, sid)


def test_hidden_by_names_a_fork_of_a_chat_trashed_by_then(store, monkeypatch):
    early = store.create_session(title="early")
    fork = store.create_session(title="early", parent_id=early)
    late = store.create_session(title="late")
    late_fork = store.create_session(title="late", parent_id=late)
    _, job_chat = kept_job(store, early, f"{MARK} job")
    trash_at(monkeypatch, store, early, DAY - 60)
    trash_at(monkeypatch, store, late, DAY + 60)
    every = {early, fork, late, late_fork, job_chat}
    assert already_hidden(store, DAY, every) == {early, fork, job_chat}
    assert already_hidden(store, DAY + 120, every) == every
    assert already_hidden(store, DAY - 120, every) == set()


def test_hidden_by_takes_column_names_only():
    with pytest.raises(ValueError):
        visibility.hidden_by("m.session_id", "0 OR 1")


def test_a_fork_of_a_chat_trashed_before_the_pass_hides_none_of_its_ideas(store, monkeypatch):
    from test_visibility_proposals import Model, listed, say

    from pi.schedule import proposals

    gone = say(store, "an old secret", at=DAY - 9000)
    fork = store.create_session(title="fork", parent_id=gone)
    say(store, "fork words", at=DAY - 8000, session=fork)
    trash_at(monkeypatch, store, gone, DAY - 60)
    say(store, "book the gym", "book the gym again", "gym once more", at=DAY - 300)
    model = Model("Gym")
    when = datetime.fromtimestamp(DAY, UTC)
    assert proposals.run_pass(store, model, now=when)["created_count"] == 1
    assert "fork words" not in model.prompts[0]
    assert listed(store) == ["Gym"]
