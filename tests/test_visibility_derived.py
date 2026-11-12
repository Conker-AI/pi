"""Chats made from a hidden chat are hidden with it; activity lists leave hidden chats out.

A fork, branch or call child (sessions.parent_id) carries its parent's title and summary, and
the chat of an AI job Conker kept from a chat is named after the job, so each is hidden while the
chat it came from is forgotten or in the Trash (pi.conversation.visibility). The child opened by
its own id still works with its own summary; the Trash view lists only what the owner put there;
restoring brings everything back. GET /activity/runs and /activity/events without a chat leave
out a hidden chat's runs and events, as the universal search's activity source does.
"""

from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_loop import Recorder, loop_with

from pi.context import project_sources
from pi.context import universal_search as search
from pi.conversation import activity, archive, visibility
from pi.conversation import history as session_history
from pi.conversation.store import Store
from pi.schedule import agent_jobs, job_models, tasks

MARK = "zqderived-5521"
DAY = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def trash(store, sid, trashed=True):
    archive.trash(store, archive.Trash(sessions=[sid], trashed=trashed))


def hidden_now(store, ids):
    with store._connect() as db:
        return visibility.hidden(db, ids)


def listed_by_sql(store):
    with store._connect() as db:
        query = f"SELECT id FROM sessions WHERE NOT {visibility.hidden_sql('id')}"
        return {row[0] for row in db.execute(query)}


def titles_found(store):
    return {row["recordId"] for row in search.conversation_metadata(store, MARK)[0]}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def tree(store):
    """A chat, its fork (title and summary copied from it, as Loop.fork does) and a fork of that."""
    parent = store.create_session(title=f"{MARK} parent")
    store.append_message(parent, "user", f"{MARK} words")
    child = store.create_session(
        title=f"{MARK} parent", parent_id=parent, summary=f"{MARK} summary of the parent"
    )
    grandchild = store.create_session(title="later", parent_id=child, summary=f"{MARK} again")
    control = store.create_session(title=f"{MARK} control")
    return parent, child, grandchild, control


def kept_job(store, session_id, name):
    """An AI job Conker made and kept from `session_id` (keep_results), and the job's own chat.

    Conker's own: a job the owner approved or adopted is his and stays visible (jobs.ADOPTED)."""
    job = agent_jobs.create(
        store,
        job_models.AgentJobCreate.model_validate(
            dict(
                name=name,
                instructions="Look around.",
                timing=dict(kind="interval", time="00:00", day=0, hours=2),
                timeZone="Asia/Jerusalem",
            )
        ),
        now=DAY.timestamp(),
        created_by="conker",
    )["id"]
    with store._connect() as db:
        db.execute(
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES (?,?,'ai_job',?,0)",
            (f"req_job_{job}", session_id, job),
        )
        db.commit()
    return agent_jobs.session_for(store, job)


def test_a_fork_of_a_trashed_chat_is_hidden_with_it_until_restored(store):
    parent, child, grandchild, control = tree(store)
    below = {child, grandchild}
    assert hidden_now(store, [parent, *below, control]) == set()
    trash(store, parent)
    assert hidden_now(store, [parent, *below, control]) == {parent, *below}
    assert listed_by_sql(store) == {control}
    assert {row["id"] for row in store.list_sessions()} == {control}
    assert titles_found(store) == {control}  # title and summary search: the sessions source
    reference = {"kind": "conversation", "sessionId": child}
    assert project_sources.resolve(store, reference) is None  # no other chat may read it
    # The Trash view lists only the chat the owner put there.
    assert [row["id"] for row in session_history.page(store, trashed="only")["results"]] == [parent]
    trash(store, parent, trashed=False)
    assert hidden_now(store, [parent, *below, control]) == set()
    assert titles_found(store) == {parent, child, grandchild, control}
    assert project_sources.resolve(store, reference) is not None


def test_trashing_a_fork_hides_what_is_below_it_but_not_its_parent(store):
    parent, child, grandchild, control = tree(store)
    trash(store, child)
    assert hidden_now(store, [parent, child, grandchild, control]) == {child, grandchild}
    assert titles_found(store) == {parent, control}


def test_a_fork_opened_by_its_own_id_still_answers_with_its_summary(store):
    parent, child, _, _ = tree(store)
    trash(store, parent)
    provider = Recorder()
    loop_with(store, provider).run_turn(child, "Carry on", request_id="derived_own_turn")
    sent = "\n".join(message.content for message in provider.calls[0])
    assert f"{MARK} summary of the parent" in sent


def test_the_chat_of_a_job_kept_from_a_hidden_chat_is_hidden_with_it(store):
    parent, child, _, control = tree(store)
    direct = kept_job(store, parent, f"{MARK} job one")
    # Kept from the fork: hidden through the fork, which is hidden through its parent.
    through_fork = kept_job(store, child, f"{MARK} job two")
    visible = kept_job(store, control, f"{MARK} job three")
    jobs = {direct, through_fork, visible}
    assert hidden_now(store, jobs) == set()
    trash(store, parent)
    assert hidden_now(store, jobs) == {direct, through_fork}
    assert titles_found(store) & jobs == {visible}
    assert [row["id"] for row in session_history.page(store, trashed="only")["results"]] == [parent]
    trash(store, parent, trashed=False)
    assert hidden_now(store, jobs) == set()
    assert titles_found(store) & jobs == jobs


def activity_chat(store, title, request_id):
    sid = store.create_session(title=title)
    run = store.start_turn(sid)
    store.complete_turn(run, "answer")
    tasks.create(
        store,
        tasks.CreateTask(request_id=request_id, session_id=sid, outcome="outcome", criteria=["x"]),
    )
    return sid, run


def statuses(page):
    return {row["session_id"]: row["content_status"] for row in page["results"]}


def test_activity_lists_leave_out_a_hidden_chat_unless_asked_for_it(store):
    hidden, hidden_run = activity_chat(store, "hidden", "derived_task_hidden")
    child = store.create_session(title="fork", parent_id=hidden)
    child_run = store.start_turn(child)
    control, _ = activity_chat(store, "control", "derived_task_control")
    trash(store, hidden)
    assert statuses(activity.list_runs(store)) == {control: "available"}
    assert set(statuses(activity.list_events(store))) == {control}
    # Asked for the chat by its id: listed, and a chat in the Trash is not "forgotten".
    assert statuses(activity.list_runs(store, session_id=hidden)) == {hidden: "available"}
    assert statuses(activity.list_events(store, session_id=hidden)) == {hidden: "available"}
    assert activity.get_run(store, hidden_run)["content_status"] == "available"
    assert activity.get_run(store, child_run)["content_status"] == "available"
    trash(store, hidden, trashed=False)
    assert statuses(activity.list_runs(store)) == {
        hidden: "available",
        child: "available",
        control: "available",
    }
    assert set(statuses(activity.list_events(store))) == {hidden, child, control}
    assert activity.get_run(store, hidden_run)["content_status"] == "available"
