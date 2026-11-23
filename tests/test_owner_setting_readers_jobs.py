"""Owner settings for jobs, webhooks, Inbox notes, decisions and research sources: each test fails if
its reader ignores the value (round two of R-F1)."""

import uuid
from contextlib import closing

import pytest
from test_agent_jobs import EVERY_TWO_HOURS, NOON, Loop, ai_job, claimed_run, reply
from test_agent_triggers import Client, Result, look, watched
from test_tool_turns import build
from test_web_research import SEARCH, SearchGate

from pi.agents import inbox
from pi.context import decisions
from pi.conversation.store import Store
from pi.core import owner_settings as owner_settings
from pi.schedule import agent_hooks, agent_jobs, job_notes, jobs

HOUR = 3600


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))


def run_claimed(store, job):
    """Claim the job's clock slot at NOON and run it; what the run returned."""
    run = claimed_run(store, job)
    loop = Loop(reply("NOTHING", agent_jobs.session_for(store, job["id"])))
    return agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON), loop


def past_run(store, job, started_at, status="completed"):
    """A finished run of the job, as if it ran at that time."""
    with store._connect() as db:
        row = db.execute(
            "SELECT revision, definition FROM scheduled_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        db.execute(
            "INSERT INTO scheduled_runs VALUES (?,?,?,?,?,?,?,NULL,?)",
            (
                "scheduled_" + uuid.uuid4().hex,
                job["id"],
                row["revision"],
                row["definition"],
                started_at,
                started_at,
                status,
                "owner-run-" + uuid.uuid4().hex,
            ),
        )


# proactivity.jobs.triggers


def test_a_trigger_the_owner_left_out_starts_nothing(store):
    choose(store, {"proactivity.jobs.triggers": ["watch", "webhook"]})
    result, loop = run_claimed(store, agent_jobs.create(store, ai_job(), now=NOON))
    assert result["status"] == "skipped" and result["code"] == "TRIGGER_OFF"
    assert loop.calls == []
    # His own Run now is never held.
    job = agent_jobs.create(store, ai_job(name="Another"), now=NOON)
    run = jobs.run_now(store, job["id"], "owner-request-" + "a" * 16, now=NOON)
    loop = Loop(reply("NOTHING", agent_jobs.session_for(store, job["id"])))
    assert agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON)["status"] == "completed"


def test_allowed_triggers_that_are_not_a_list_allow_every_trigger(store):
    choose(store, {"proactivity.jobs.triggers": "time"})
    result, _ = run_claimed(store, agent_jobs.create(store, ai_job(), now=NOON))
    assert result["status"] == "completed"


# proactivity.jobs.max_runs


def test_runs_a_day_hold_back_a_job_that_already_ran_enough(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    past_run(store, job, NOON - 5 * HOUR)
    past_run(store, job, NOON - 3 * HOUR)
    past_run(store, job, NOON - 25 * HOUR)  # more than a day ago: does not count
    past_run(store, job, NOON - 4 * HOUR, status="skipped")  # did nothing: does not count
    choose(store, {"proactivity.jobs.max_runs": 2})
    result, loop = run_claimed(store, job)
    assert result["status"] == "skipped" and result["code"] == "RUN_LIMIT"
    assert loop.calls == []


def test_runs_a_day_under_the_limit_run(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    past_run(store, job, NOON - 5 * HOUR)
    past_run(store, job, NOON - 3 * HOUR)
    choose(store, {"proactivity.jobs.max_runs": 3})
    assert run_claimed(store, job)[0]["status"] == "completed"


# proactivity.jobs.min_interval


def test_the_shortest_interval_holds_back_a_clock_run_too_soon(store):
    job = agent_jobs.create(store, ai_job(timing=EVERY_TWO_HOURS), now=NOON)
    past_run(store, job, NOON - 2 * HOUR)
    choose(store, {"proactivity.jobs.min_interval": 180})
    result, loop = run_claimed(store, job)
    assert result["status"] == "skipped" and result["code"] == "TOO_SOON"
    assert loop.calls == []


def test_the_default_shortest_interval_lets_a_two_hour_job_run(store):
    job = agent_jobs.create(store, ai_job(timing=EVERY_TWO_HOURS), now=NOON)
    past_run(store, job, NOON - 2 * HOUR)
    assert run_claimed(store, job)[0]["status"] == "completed"


def test_a_watch_never_looks_more_often_than_the_shortest_interval(store):
    choose(store, {"proactivity.jobs.min_interval": 60})
    watched(store, everyMinutes=5)
    assert look(store, Client(Result(True, {"messages": []})), NOON) == ["baseline"]
    with store._connect() as db:
        [next_check] = db.execute("SELECT next_check FROM agent_job_watch").fetchone()
    assert next_check == NOON + HOUR


# proactivity.hooks.rate, proactivity.hooks.size


def hooked(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    return job, agent_hooks.issue(store, job["id"], now=NOON)["token"]


def test_webhook_calls_an_hour_follow_the_owner(store):
    job, token = hooked(store)
    choose(store, {"proactivity.hooks.rate": 1})
    assert agent_hooks.receive(store, job["id"], token, {}, now=NOON)["started"] is True
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='completed'")
    with pytest.raises(agent_hooks.HookError) as refused:
        agent_hooks.receive(store, job["id"], token, {}, now=NOON + 60)
    assert refused.value.code == "hook_rate" and refused.value.status == 429
    # An hour later the call is accepted again.
    assert agent_hooks.receive(store, job["id"], token, {}, now=NOON + HOUR + 1)["started"]


def test_the_largest_webhook_payload_follows_the_owner(store):
    job, token = hooked(store)
    body = {"text": "x" * 2000}
    choose(store, {"proactivity.hooks.size": 1})
    with pytest.raises(agent_hooks.HookError) as refused:
        agent_hooks.receive(store, job["id"], token, body, now=NOON)
    assert refused.value.code == "hook_too_large" and refused.value.status == 413
    choose(store, {"proactivity.hooks.size": 3})
    assert agent_hooks.receive(store, job["id"], token, body, now=NOON)["started"] is True


# inbox.notes_days


def test_notes_older_than_the_owners_days_are_hidden_never_deleted(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    run = claimed_run(store, job)
    loop = Loop(reply("Physics exam on Thursday.", agent_jobs.session_for(store, job["id"])))
    assert agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON)["noted"] is True
    [note] = job_notes.notes(store, now=NOON)
    later = NOON + 3 * 86400
    choose(store, {"inbox.notes_days": 2})
    assert job_notes.notes(store, now=later) == []
    assert all(item["kind"] != "agent-job" for item in inbox.inbox(store, now=later)["needsYou"])
    with store._connect() as db:
        assert db.execute("SELECT 1 FROM notices WHERE id=?", (note["id"],)).fetchone()
    choose(store, {"inbox.notes_days": 5})
    assert [item["id"] for item in job_notes.notes(store, now=later)] == [note["id"]]
    # Unset: fourteen days.
    choose(store, {})
    assert job_notes.notes(store, now=NOON + 13 * 86400)
    assert job_notes.notes(store, now=NOON + 15 * 86400) == []


# context.decisions


def add(store, session, kind, author):
    revision = decisions.load(store, session)["revision"]
    body = decisions.Add(expected_revision=revision, kind=kind, text="Use SQLite", author=author)
    return decisions.add(store, session, body)


def test_only_the_owner_adds_decisions_when_he_says_so(store):
    session = store.create_session()
    choose(store, {"context.decisions": "owner"})
    with pytest.raises(decisions.DecisionError) as refused:
        add(store, session, "decision", "ai")
    assert refused.value.code == "owner_decides"
    add(store, session, "thread", "ai")
    add(store, session, "decision", "owner")
    choose(store, {"context.decisions": "both"})
    add(store, session, "decision", "ai")


# research.sources


@pytest.mark.parametrize(("chosen", "told"), [(3, 3), (None, 5), (50, 20)])
def test_a_research_turn_is_told_the_owners_sources_per_answer(store, chosen, told):
    if chosen is not None:
        choose(store, {"research.sources": chosen})
    loop, provider = build(store, [SEARCH, "Summary: https://example.org/source"], SearchGate())
    loop.run_turn(store.create_session(), "Research databases", research_mode="web")
    assert any(f"Cite at most {told} sources." in m.content for m in provider.sent[0])
