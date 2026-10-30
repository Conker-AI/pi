"""AI jobs: a cron job runs its agent on the owner's instructions; tools ask exactly as in a chat."""

import hashlib
from contextlib import closing
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi.agents import inbox
from pi.api import server as api
from pi.api.browser_contract import owner_allowed, session_only_write
from pi.conversation.store import Store
from pi.schedule import agent_jobs, job_models, jobs
from pi.turns.failures import TurnFailed

OWNER = "agent-jobs-owner-control-key-" + "o" * 32
# 12:00 in Jerusalem is outside the default quiet hours (22:00-07:00); 02:00 is inside.
NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()
NIGHT = datetime.fromisoformat("2026-10-26T00:00:00+00:00").timestamp()
EVERY_TWO_HOURS = dict(kind="interval", time="00:00", day=0, hours=2)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "agent-jobs.db")) as store:
        yield store


def ai_job(**changes):
    value = dict(
        name="Watch my week",
        instructions="Look for exams, deadlines and anything I should prepare.",
        timing=EVERY_TWO_HOURS,
        timeZone="Asia/Jerusalem",
    )
    value.update(changes)
    return job_models.AgentJobCreate.model_validate(value)


class Loop:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def run_turn(self, session_id, text, **kwargs):
        self.calls.append((session_id, text, kwargs))
        if self.error:
            raise self.error
        return self.result


def reply(text, session_id):
    return {
        "session_id": session_id,
        "turn_id": "trn_" + "1" * 16,
        "message": {"id": "msg_" + "2" * 16, "content": text},
    }


def claimed_run(store, job, now=NOON):
    with store._connect() as db:
        db.execute("UPDATE scheduled_jobs SET next_at=? WHERE id=?", (now - 1, job["id"]))
    [run] = jobs.claim_due(store, now=now)
    return run


def test_an_ai_job_is_an_agent_job_with_its_own_chat(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    view = jobs.browser_job(job)
    assert view.definition.target.kind == "agent"
    assert view.definition.timing.kind == "interval" and view.definition.timing.hours == 2
    session = store.get_session(view.definition.target.sessionId)
    assert session["status"] == "open" and session["title"] == "Watch my week"


def test_any_timing_works_including_a_weekly_review(store):
    weekly = dict(kind="weekly", time="21:00", day=0, hours=1)
    job = agent_jobs.create(store, ai_job(name="Weekly review", timing=weekly), now=NOON)
    assert jobs.browser_job(job).definition.timing.kind == "weekly"


def test_an_ai_job_never_carries_a_job_budget():
    with pytest.raises(ValidationError):
        job_models.Definition.model_validate(
            dict(
                name="x",
                instructions="y",
                agentId="companion",
                timing=EVERY_TWO_HOURS,
                timeZone="UTC",
                enabled=True,
                requireBudget=True,
                target=dict(kind="agent"),
            )
        )


def test_a_finding_becomes_one_inbox_note(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    session = agent_jobs.session_for(store, job["id"])
    run = claimed_run(store, job)
    text = "**Physics exam on Thursday.**\nI can make notes and practice questions."
    loop = Loop(reply(text, session))
    outcome = agent_jobs.AgentJobs(store, loop)(
        run["definition"]["target"], action_id=run["id"], agent_id="companion"
    )
    assert outcome["status"] == "completed" and outcome["noted"] is True
    # The run ID is the turn's request ID, so a repeated dispatch replays instead of running twice.
    [(used_session, sent, kwargs)] = loop.calls
    assert used_session == session and kwargs == {"request_id": run["id"]}
    assert sent.startswith(agent_jobs.PREAMBLE) and sent.endswith(ai_job().instructions)
    [note] = agent_jobs.notes(store)
    assert note["title"] == "Physics exam on Thursday."
    assert note["detail"] == "I can make notes and practice questions."
    assert note["sessionId"] == session
    kinds = [item["kind"] for item in inbox.inbox(store)["needsYou"]]
    assert kinds.count("agent-job") == 1
    assert agent_jobs.dismiss(store, note["id"])["dismissed"] is True
    assert agent_jobs.notes(store) == []


def test_a_run_is_told_its_date_and_time_in_the_jobs_zone(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    run = claimed_run(store, job)
    loop = Loop(reply("NOTHING", agent_jobs.session_for(store, job["id"])))
    # Dispatched late, then again: both say when Pi took the run up, so the second one replays.
    agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON + 7200)
    agent_jobs.AgentJobs(store, loop).run(run["id"], now=NOON + 86400)
    [(_, first, _), (_, again, _)] = loop.calls
    line = (
        "This run started on Monday, 26 October 2026 (2026-10-26) at 12:00, Asia/Jerusalem time "
        "(the job's time zone)."
    )
    assert first == again and first == f"{agent_jobs.PREAMBLE}\n\n{line}\n\n{ai_job().instructions}"


def test_the_date_follows_the_jobs_own_zone():
    instant = datetime.fromisoformat("2026-11-01T23:30:00+00:00").timestamp()
    assert agent_jobs.when(instant, "Asia/Tokyo").startswith(
        "This run started on Monday, 2 November 2026 (2026-11-02) at 08:30, Asia/Tokyo time"
    )
    assert agent_jobs.when(instant, "America/Los_Angeles").startswith(
        "This run started on Sunday, 1 November 2026 (2026-11-01) at 15:30,"
    )


@pytest.mark.parametrize("text", ["NOTHING", "nothing.", "**NOTHING**", "   "])
def test_nothing_stays_quiet(store, text):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    run = claimed_run(store, job)
    loop = Loop(reply(text, agent_jobs.session_for(store, job["id"])))
    outcome = agent_jobs.AgentJobs(store, loop).run(run["id"])
    assert outcome["status"] == "completed" and outcome["noted"] is False
    assert agent_jobs.notes(store) == []


def test_a_set_tool_waits_in_the_chat_and_a_failure_is_honest(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    session = agent_jobs.session_for(store, job["id"])
    run = claimed_run(store, job)
    parked = {"session_id": session, "turn_id": "trn_x", "status": "awaiting_approval"}
    assert agent_jobs.AgentJobs(store, Loop(parked)).run(run["id"])["waitingForApproval"] is True
    failed = agent_jobs.AgentJobs(store, Loop(error=TurnFailed("model down"))).run(run["id"])
    assert failed["status"] == "failed" and "model down" not in failed["reason"]
    assert agent_jobs.notes(store) == []


def test_other_targets_go_to_toolgate_unchanged(store):
    calls = []

    def published(target, **kwargs):
        calls.append((target, kwargs))
        return {"status": "completed"}

    executor = agent_jobs.AgentJobs(store, Loop(), published)
    target = {
        "kind": "tool",
        "id": "calendar.read",
        "publishedVersion": 1,
        "digest": "a" * 64,
        "args": {},
    }
    run_id = "scheduled_" + "3" * 32
    assert executor(target, action_id=run_id, agent_id="companion")["status"] == "completed"
    assert calls == [(target, {"action_id": run_id, "agent_id": "companion"})]


def test_a_closed_chat_is_replaced_not_reopened(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    old = agent_jobs.session_for(store, job["id"])
    store.close_session(old, "closed")
    run = claimed_run(store, job)
    loop = Loop(reply("NOTHING", "ses_" + "9" * 16))
    agent_jobs.AgentJobs(store, loop).run(run["id"])
    new = agent_jobs.session_for(store, job["id"])
    assert new != old and loop.calls[0][0] == new


def test_repeating_jobs_skip_quiet_hours_and_timed_ones_run(store):
    repeating = agent_jobs.create(store, ai_job(), now=NOON)
    nightly = agent_jobs.create(
        store,
        ai_job(name="Late review", timing=dict(kind="daily", time="02:00", day=0, hours=1)),
        now=NOON,
    )
    with store._connect() as db:
        db.execute("UPDATE scheduled_jobs SET next_at=?", (NIGHT - 1,))
    claimed = jobs.claim_due(store, now=NIGHT)
    assert [run["job_id"] for run in claimed] == [nightly["id"]]
    assert jobs.get_job(store, repeating["id"])["next_at"] > NIGHT


def test_the_browser_gets_ai_jobs_only_never_raw_tool_jobs(store, monkeypatch):
    assert owner_allowed("POST", "/jobs/agent")
    assert not owner_allowed("POST", "/jobs")
    assert owner_allowed("GET", "/jobs/agent/notes")
    note = "/jobs/agent/notes/note_" + "a" * 32 + "/dismiss"
    assert owner_allowed("POST", note) and session_only_write("POST", note)
    assert not session_only_write("POST", "/jobs/agent")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    digest = hashlib.sha256(OWNER.encode()).hexdigest()
    monkeypatch.setattr(api.app.state, "owner_key_hash", digest, raising=False)
    client = TestClient(api.app)
    owner = {"X-Pi-Owner-Key": OWNER}
    created = client.post("/jobs/agent", headers=owner, json=ai_job().model_dump())
    assert created.status_code == 201
    assert created.json()["definition"]["target"]["kind"] == "agent"
    # The owner's job page shows what it does and which agent does it.
    shown = client.get(f"/jobs/{created.json()['id']}", headers=owner).json()["definition"]
    assert shown["instructions"] == ai_job().instructions and shown["instructionsConfigured"]
    assert (shown["agentId"], shown["agentName"]) == ("companion", "Conker")
    assert client.post("/jobs", headers=owner, json={}).status_code == 403
    empty = {"schemaVersion": 1, "results": []}
    assert client.get("/jobs/agent/notes", headers=owner).json() == empty
    missing = "/jobs/agent/notes/note_" + "b" * 32 + "/dismiss"
    assert client.post(missing, headers=owner).status_code == 404


def test_summaries_are_one_plain_line():
    assert agent_jobs.summarize("# Rent is due tomorrow\nPay it in the bank app.") == (
        "Rent is due tomorrow",
        "Pay it in the bank app.",
    )
    assert agent_jobs.summarize("NOTHING") is None
    assert len(agent_jobs.summarize("x" * 500)[0]) == agent_jobs.MAX_TITLE


def test_the_ideas_pass_ignores_ai_job_chats(store):
    from pi.schedule import proposals

    job = agent_jobs.create(store, ai_job(), now=NOON)
    session = agent_jobs.session_for(store, job["id"])
    store.append_message(session, "user", "Look for exams and deadlines.")
    other = store.create_session(title="Plan")
    store.append_message(other, "user", "Remind me about judo.")
    with store._connect() as db:
        found = proposals._evidence(db, 0, 1e12)
    assert [row["session_id"] for row in found] == [other]


def test_forgetting_the_chat_clears_its_notes_and_starts_fresh(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    session = agent_jobs.session_for(store, job["id"])
    run = claimed_run(store, job)
    agent_jobs.AgentJobs(store, Loop(reply("Bill due Friday.", session))).run(run["id"])
    assert agent_jobs.notes(store)
    with store._connect() as db:
        agent_jobs.redact(db, [session])
    assert agent_jobs.notes(store) == [] and agent_jobs.session_for(store, job["id"]) is None
