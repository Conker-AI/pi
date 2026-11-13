"""Restoring, kept jobs, the unreplied queue and activity lookups under the hidden-chat rule.

A chat made from a chat in the Trash is hidden with it, so restoring it alone is refused
(`restore_parent_first`) instead of leaving it listed nowhere. An AI job Conker kept from a hidden
chat does not run until the chat is restored or the owner adopts the job (pi.schedule.job_hidden).
GET /turns/unreplied leaves out a hidden chat's turns; a task or run in a trashed chat asked for by
its own id keeps its activity events (pi.conversation.visibility).
"""

from contextlib import closing
from datetime import datetime
from types import SimpleNamespace

import pytest

from pi.conversation import activity, archive
from pi.conversation import history as session_history
from pi.conversation.store import Store
from pi.schedule import agent_jobs, job_models, jobs, tasks

# 12:00 in Jerusalem, outside the default quiet hours.
NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()
DAILY = dict(kind="daily", time="13:00", day=0, hours=1)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, ids, trashed=True):
    return archive.trash(store, archive.Trash(sessions=list(ids), trashed=trashed))


def in_trash(store):
    return {row["id"] for row in session_history.page(store, trashed="only")["results"]}


def listed(store):
    return {row["id"] for row in store.list_sessions()}


def family(store):
    parent = store.create_session(title="parent")
    child = store.create_session(title="fork", parent_id=parent)
    grandchild = store.create_session(title="fork of fork", parent_id=child)
    return parent, child, grandchild


# 1. Restoring a chat whose parent is still in the Trash.


@pytest.mark.parametrize("order", ["parent_first", "child_first"])
def test_restoring_a_fork_while_its_parent_is_in_the_trash_is_refused(store, order):
    parent, child, _ = family(store)
    # With its parent visible, a fork restores alone.
    trash(store, [child])
    trash(store, [child], trashed=False)
    trash(store, [child])
    trash(store, [parent])
    with pytest.raises(archive.ArchiveError) as refused:
        trash(store, [child], trashed=False)
    assert refused.value.code == "restore_parent_first" and refused.value.status == 409
    assert refused.value.detail["sessions"] == [child]
    assert refused.value.detail["parents"] == [parent]
    assert in_trash(store) == {parent, child}  # nothing changed
    # Both in one request work in either order.
    ids = [parent, child] if order == "parent_first" else [child, parent]
    assert trash(store, ids, trashed=False)["sessions"] == ids
    assert in_trash(store) == set()
    assert {parent, child} <= listed(store)


def test_restoring_the_parent_first_then_the_fork_works(store):
    parent, child, _ = family(store)
    trash(store, [child])
    trash(store, [parent])
    with pytest.raises(archive.ArchiveError):
        trash(store, [child], trashed=False)
    trash(store, [parent], trashed=False)
    trash(store, [child], trashed=False)
    assert in_trash(store) == set()
    assert {parent, child} <= listed(store)


def test_a_grandparent_in_the_trash_refuses_the_whole_request(store):
    parent, _, grandchild = family(store)
    other = store.create_session(title="unrelated")
    trash(store, [grandchild, other])
    trash(store, [parent])
    with pytest.raises(archive.ArchiveError) as refused:
        trash(store, [other, grandchild], trashed=False)
    assert refused.value.detail["sessions"] == [grandchild]
    assert refused.value.detail["parents"] == [parent]
    assert in_trash(store) == {parent, grandchild, other}  # all or nothing


# 2. A job Conker kept from a hidden chat does not run.


def kept_job(store, session_id, name="Kept job", **extra):
    """An AI job Conker made and kept from `session_id`, set for 13:00 Jerusalem time."""
    fields = dict(name=name, instructions="Look around.", timing=DAILY, timeZone="Asia/Jerusalem")
    body = job_models.AgentJobCreate.model_validate({**fields, **extra})
    job = agent_jobs.create(store, body, now=NOON, created_by="conker")
    with store._connect() as db:
        db.execute(
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES (?,?,'ai_job',?,0)",
            (f"req_job_{job['id']}", session_id, job["id"]),
        )
        db.commit()
    return job


def due(store, job_id, now):
    with store._connect() as db:
        db.execute("UPDATE scheduled_jobs SET next_at=? WHERE id=?", (now - 1, job_id))
        db.commit()


def claimed(store, job_id, now):
    due(store, job_id, now)
    return [run["job_id"] for run in jobs.claim_due(store, now=now)]


def run_count(store, job_id):
    with store._connect() as db:
        query = "SELECT COUNT(*) FROM scheduled_runs WHERE job_id=?"
        return db.execute(query, (job_id,)).fetchone()[0]


def test_a_kept_job_does_not_fire_while_its_chat_is_in_the_trash(store):
    source = store.create_session(title="source")
    job = kept_job(store, source)
    trash(store, [source])
    assert claimed(store, job["id"], NOON) == []
    assert run_count(store, job["id"]) == 0
    # The slot passed: it does not wait to fire the moment the chat comes back.
    assert jobs.get_job(store, job["id"])["next_at"] > NOON
    trash(store, [source], trashed=False)
    assert claimed(store, job["id"], NOON + 86400) == [job["id"]]


def test_run_now_refuses_a_kept_job_while_its_chat_is_hidden(store):
    source = store.create_session(title="source")
    job = kept_job(store, source)
    trash(store, [source])
    with pytest.raises(jobs.JobError) as refused:
        jobs.run_now(store, job["id"], "manual_run_0000000001", now=NOON)
    assert refused.value.code == "job_source_hidden" and refused.value.status == 409
    assert run_count(store, job["id"]) == 0
    trash(store, [source], trashed=False)
    run = jobs.run_now(store, job["id"], "manual_run_0000000002", now=NOON)
    assert run["replayed"] is False and run_count(store, job["id"]) == 1


def test_a_job_kept_from_a_fork_of_a_trashed_chat_does_not_run(store):
    parent, child, _ = family(store)
    job = kept_job(store, child)
    trash(store, [parent])
    assert claimed(store, job["id"], NOON) == []
    with pytest.raises(jobs.JobError):
        jobs.run_now(store, job["id"], "manual_run_0000000003", now=NOON)


def test_an_adopted_job_runs_while_its_source_chat_is_hidden(store):
    source = store.create_session(title="source")
    adopted, kept = kept_job(store, source, "Adopted"), kept_job(store, source, "Kept")
    agent_jobs.adopt(store, adopted["id"], job_models.Adopt(expected_revision=adopted["revision"]))
    trash(store, [source])
    due(store, kept["id"], NOON)
    assert claimed(store, adopted["id"], NOON) == [adopted["id"]]
    assert run_count(store, kept["id"]) == 0


class Loop:
    def __init__(self):
        self.calls = []

    def run_turn(self, session_id, text, **kwargs):
        self.calls.append(session_id)
        return {"session_id": session_id, "turn_id": None, "message": {"content": "NOTHING"}}


def test_a_run_admitted_before_its_chat_was_trashed_is_held_before_its_turn(store):
    source = store.create_session(title="source")
    job = kept_job(store, source)
    run = jobs.run_now(store, job["id"], "manual_run_0000000004", now=NOON)
    trash(store, [source])
    loop = Loop()
    outcome = agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON)
    assert outcome["status"] == "failed" and outcome["code"] == "SOURCE_HIDDEN"
    assert loop.calls == []


class Watched:
    """A ToolGate client whose watch tool returns something new on every look."""

    def __init__(self):
        self.calls = []
        self.clients = {"companion": self}

    def invoke(self, tool_id, args, **kwargs):
        self.calls.append(tool_id)
        return SimpleNamespace(ok=True, result={"looks": len(self.calls)})


def watched_job(store, session_id, tool_id="mcp.telegram.messages"):
    """A kept job that looks at a tool every 5 minutes and starts when its answer changes."""
    watch = dict(toolId=tool_id, args={}, everyMinutes=5)
    timing = dict(kind="interval", time="00:00", day=0, hours=24)
    return kept_job(store, session_id, tool_id, timing=timing, watch=watch)


def watch_row(store, job_id):
    with store._connect() as db:
        query = "SELECT digest, checked_at FROM agent_job_watch WHERE job_id=?"
        return tuple(db.execute(query, (job_id,)).fetchone())


def test_a_kept_watched_job_calls_no_tool_while_its_chat_is_hidden(store):
    source = store.create_session(title="source")
    job = watched_job(store, source)
    # An adopted job is the owner's: it keeps looking whatever happens to the chat.
    adopted = watched_job(store, source, "mcp.mail.inbox")
    agent_jobs.adopt(store, adopted["id"], job_models.Adopt(expected_revision=adopted["revision"]))
    client = Watched()
    worker = agent_jobs.AgentJobs(store, loop=None, published=client)
    assert worker.watch(NOON) == ["baseline", "baseline"]
    before = watch_row(store, job["id"])
    trash(store, [source])
    # Each due look while hidden is a pause: no tool call, the last real look kept.
    for later in (NOON + 600, NOON + 1200):
        assert "paused" in worker.watch(later)
    assert client.calls.count("mcp.telegram.messages") == 1
    assert client.calls.count("mcp.mail.inbox") == 3
    assert watch_row(store, job["id"]) == before
    assert run_count(store, job["id"]) == 0
    # Restored, the next look calls the tool once and the change starts the job.
    trash(store, [source], trashed=False)
    worker.watch(NOON + 1800)
    assert client.calls.count("mcp.telegram.messages") == 2
    assert run_count(store, job["id"]) == 1


# 3. The unreplied queue.


def acted_turn(store, session_id):
    turn = store.start_turn(session_id)
    store.finish_turn(turn, "interrupted", acted=1)
    return turn


def test_unreplied_turns_leave_out_trashed_chats_and_their_forks(store):
    parent, child, _ = family(store)
    control = store.create_session(title="control")
    turns = {acted_turn(store, sid): sid for sid in (parent, child, control)}
    assert {row["id"] for row in store.acted_without_reply()} == set(turns)
    trash(store, [parent])
    assert [row["session_id"] for row in store.acted_without_reply()] == [control]
    trash(store, [parent], trashed=False)
    assert {row["id"] for row in store.acted_without_reply()} == set(turns)


# 4. Activity asked for one task or run.


def test_a_task_or_run_in_a_trashed_chat_keeps_its_activity_when_asked_by_id(store):
    sid = store.create_session(title="hidden")
    run = store.start_turn(sid)
    store.complete_turn(run, "answer")
    task = tasks.create(
        store,
        tasks.CreateTask(
            request_id="restore_jobs_task", session_id=sid, outcome="o", criteria=["x"]
        ),
    )["id"]
    with store._connect() as db:
        db.execute("INSERT INTO task_runs VALUES(?,?)", (task, run))
        db.commit()
    by_task = activity.list_events(store, task_id=task)["results"]
    by_run = activity.list_events(store, run_id=run)["results"]
    assert {row["kind"] for row in by_task} >= {"task_created", "run_started"}
    assert {row["kind"] for row in by_run} == {"run_started", "run_status"}
    trash(store, [sid])
    assert activity.list_events(store)["results"] == []  # an unscoped list: hidden
    assert activity.list_runs(store)["results"] == []
    assert activity.list_events(store, task_id=task)["results"] == by_task
    assert activity.list_events(store, run_id=run)["results"] == by_run
    assert [row["id"] for row in activity.list_runs(store, task_id=task)["results"]] == [run]
