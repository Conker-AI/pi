"""L3: the Morning check-in's model input lists the open loops due today or overdue (R-M7)."""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from test_tool_turns import FakeGate, build

from pi.context import loop_briefing, open_loops
from pi.conversation import archive
from pi.conversation.store import Store
from pi.schedule import agent_jobs, jobs, rhythm
from pi.schedule.job_models import AgentJobCreate, Timing

NOON = datetime(2026, 10, 29, 10, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def day(store, offset):
    return (date.fromisoformat(open_loops.today(store)) + timedelta(days=offset)).isoformat()


def add(store, session, text, due=None, by="owner"):
    body = open_loops.LoopAdd(session_id=session, text=text, promised_by=by, due_on=due)
    return open_loops.add(store, body)["loop"]


def run_text(store, job_id, run_id, executor):
    run = jobs.run_now(store, job_id, run_id, now=NOON)
    executor.run(run["id"], now=NOON)
    session = agent_jobs.session_for(store, job_id)
    return next(m for m in store.messages(session) if m["role"] == "user")["content"]


def checkin(store):
    job = rhythm.seed(store, now=NOON)
    loop, _ = build(store, ["NOTHING"], FakeGate())
    return job, agent_jobs.AgentJobs(store, loop, briefing=loop_briefing.for_job)


def test_the_input_lists_what_is_due_today_or_overdue_and_nothing_else(store):
    chat = store.create_session()
    add(store, chat, "Send Maya the invoice", day(store, -3))
    add(store, chat, "Call the plumber", day(store, 0))
    add(store, chat, "Check the build tomorrow", day(store, 1), by="conker")
    add(store, chat, "Learn Greek")
    done = add(store, chat, "Already finished", day(store, -1))
    open_loops.change_state(store, done["id"], open_loops.LoopState(state="done"))
    job, executor = checkin(store)
    text = run_text(store, job["id"], "morning-loops-run-0001", executor)
    assert "Send Maya the invoice" in text and "Call the plumber" in text
    assert "overdue" in text and day(store, -3) in text
    assert "Check the build tomorrow" not in text and "Learn Greek" not in text
    assert "Already finished" not in text
    assert "not instructions" in text  # the words are data


def test_a_hidden_chat_adds_nothing_and_no_loops_adds_no_section(store):
    chat = store.create_session()
    add(store, chat, "Secret promise from a trashed chat", day(store, -1))
    archive.trash(store, archive.Trash(sessions=[chat], trashed=True))
    job, executor = checkin(store)
    text = run_text(store, job["id"], "morning-loops-run-0002", executor)
    assert "Secret promise" not in text and "Open loops" not in text


def test_the_list_is_capped_and_says_more_exist(store):
    chat = store.create_session()
    for index in range(loop_briefing.MAX_LISTED + 2):
        add(store, chat, f"Promise number {index:02d}", day(store, -1))
    job, executor = checkin(store)
    text = run_text(store, job["id"], "morning-loops-run-0003", executor)
    assert text.count("Promise number") == loop_briefing.MAX_LISTED
    assert "more" in text


def test_other_jobs_get_no_loops(store):
    chat = store.create_session()
    add(store, chat, "Send Maya the invoice", day(store, -3))
    mine = AgentJobCreate(
        name="Mine",
        instructions="Look at my week.",
        timing=Timing(kind="daily", time="09:00", day=0, hours=24),
        timeZone="Asia/Jerusalem",
    )
    job = agent_jobs.create(store, mine, now=NOON)
    loop, _ = build(store, ["NOTHING"], FakeGate())
    executor = agent_jobs.AgentJobs(store, loop, briefing=loop_briefing.for_job)
    text = run_text(store, job["id"], "morning-loops-run-0004", executor)
    assert "Send Maya" not in text
