"""AI checks: a cron job runs its agent in its own chat; tools ask exactly as they do in a chat."""

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
from pi.owner import preferences as prefs
from pi.schedule import agent_checks, job_models, jobs
from pi.turns.failures import TurnFailed

OWNER = "checks-owner-control-key-" + "o" * 32
# 12:00 in Jerusalem is outside the default quiet hours (22:00-07:00); 02:00 is inside.
NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()
NIGHT = datetime.fromisoformat("2026-10-26T00:00:00+00:00").timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "checks.db")) as store:
        yield store


def check(**changes):
    value = dict(
        name="Watch my week",
        instructions="Look for exams, deadlines and anything I should prepare.",
        hours=2,
        timeZone="Asia/Jerusalem",
    )
    value.update(changes)
    return job_models.CheckCreate.model_validate(value)


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


def test_a_check_is_an_agent_job_with_its_own_chat(store):
    job = agent_checks.create_check(store, check(), now=NOON)
    view = jobs.browser_job(job)
    assert view.definition.target.kind == "agent"
    assert view.definition.timing.kind == "interval" and view.definition.timing.hours == 2
    session = store.get_session(view.definition.target.sessionId)
    assert session["status"] == "open" and session["title"] == "Watch my week"


def test_a_check_never_carries_a_job_budget():
    with pytest.raises(ValidationError):
        job_models.Definition.model_validate(
            dict(
                name="x",
                instructions="y",
                agentId="companion",
                timing=dict(kind="interval", time="00:00", day=0, hours=2),
                timeZone="UTC",
                enabled=True,
                requireBudget=True,
                target=dict(kind="agent"),
            )
        )


def test_a_finding_becomes_one_inbox_note(store):
    job = agent_checks.create_check(store, check(), now=NOON)
    session = agent_checks.session_for(store, job["id"])
    run = claimed_run(store, job)
    loop = Loop(
        reply("**Physics exam on Thursday.**\nI can make notes and practice questions.", session)
    )
    executor = agent_checks.AgentChecks(store, loop)
    outcome = executor(run["definition"]["target"], action_id=run["id"], agent_id="companion")
    assert outcome["status"] == "completed" and outcome["noted"] is True
    # The run ID is the turn's request ID, so a repeated dispatch replays instead of running twice.
    [(used_session, text, kwargs)] = loop.calls
    assert used_session == session and kwargs == {"request_id": run["id"]}
    assert text.startswith(agent_checks.PREAMBLE) and text.endswith(check().instructions)
    [note] = agent_checks.notes(store)
    assert note["title"] == "Physics exam on Thursday."
    assert note["detail"] == "I can make notes and practice questions."
    assert note["sessionId"] == session
    assert [item["kind"] for item in inbox.inbox(store)["needsYou"] if item["kind"] == "check"] == [
        "check"
    ]
    assert agent_checks.dismiss(store, note["id"])["dismissed"] is True
    assert agent_checks.notes(store) == []


@pytest.mark.parametrize("text", ["NOTHING", "nothing.", "**NOTHING**", "   "])
def test_nothing_stays_quiet(store, text):
    job = agent_checks.create_check(store, check(), now=NOON)
    run = claimed_run(store, job)
    loop = Loop(reply(text, agent_checks.session_for(store, job["id"])))
    outcome = agent_checks.AgentChecks(store, loop).run(run["id"])
    assert outcome == {**outcome, "status": "completed", "noted": False}
    assert agent_checks.notes(store) == []


def test_a_set_tool_waits_in_the_chat_and_a_failure_is_honest(store):
    job = agent_checks.create_check(store, check(), now=NOON)
    session = agent_checks.session_for(store, job["id"])
    run = claimed_run(store, job)
    parked = agent_checks.AgentChecks(
        store, Loop({"session_id": session, "turn_id": "trn_x", "status": "awaiting_approval"})
    )
    assert parked.run(run["id"])["waitingForApproval"] is True
    failed = agent_checks.AgentChecks(store, Loop(error=TurnFailed("model down"))).run(run["id"])
    assert failed["status"] == "failed" and "model down" not in failed["reason"]
    assert agent_checks.notes(store) == []


def test_other_targets_go_to_toolgate_unchanged(store):
    calls = []

    def published(target, **kwargs):
        calls.append((target, kwargs))
        return {"status": "completed"}

    executor = agent_checks.AgentChecks(store, Loop(), published)
    target = {
        "kind": "tool",
        "id": "calendar.read",
        "publishedVersion": 1,
        "digest": "a" * 64,
        "args": {},
    }
    assert (
        executor(target, action_id="scheduled_" + "3" * 32, agent_id="companion")["status"]
        == "completed"
    )
    assert calls == [(target, {"action_id": "scheduled_" + "3" * 32, "agent_id": "companion"})]


def test_a_forgotten_chat_is_replaced_not_reopened(store):
    job = agent_checks.create_check(store, check(), now=NOON)
    old = agent_checks.session_for(store, job["id"])
    store.close_session(old, "closed")
    run = claimed_run(store, job)
    loop = Loop(reply("NOTHING", "ses_" + "9" * 16))
    agent_checks.AgentChecks(store, loop).run(run["id"])
    new = agent_checks.session_for(store, job["id"])
    assert new != old and loop.calls[0][0] == new


def test_quiet_hours_skip_the_slot_without_running(store):
    job = agent_checks.create_check(store, check(), now=NOON)
    with store._connect() as db:
        db.execute("UPDATE scheduled_jobs SET next_at=? WHERE id=?", (NIGHT - 1, job["id"]))
    assert jobs.claim_due(store, now=NIGHT) == []
    assert jobs.get_job(store, job["id"])["next_at"] > NIGHT
    preferences = prefs.load(store)["preferences"]
    assert preferences["quietHours"]["start"] == "22:00"
    assert (
        len(jobs.claim_due(store, now=jobs.get_job(store, job["id"])["next_at"] + NOON - NIGHT))
        <= 1
    )


def test_the_browser_gets_checks_only_never_raw_tool_jobs(store, monkeypatch):
    assert owner_allowed("POST", "/jobs/checks")
    assert not owner_allowed("POST", "/jobs")
    assert owner_allowed("GET", "/jobs/checks/notes")
    note = "/jobs/checks/notes/note_" + "a" * 32 + "/dismiss"
    assert owner_allowed("POST", note) and session_only_write("POST", note)
    assert not session_only_write("POST", "/jobs/checks")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    client = TestClient(api.app)
    owner = {"X-Pi-Owner-Key": OWNER}
    created = client.post("/jobs/checks", headers=owner, json=check().model_dump())
    assert created.status_code == 201 and created.json()["definition"]["target"]["kind"] == "agent"
    raw = client.post("/jobs", headers=owner, json={})
    assert raw.status_code == 403
    assert client.get("/jobs/checks/notes", headers=owner).json() == {
        "schemaVersion": 1,
        "results": [],
    }
    assert (
        client.post("/jobs/checks/notes/note_" + "b" * 32 + "/dismiss", headers=owner).status_code
        == 404
    )


def test_summaries_are_one_plain_line():
    assert agent_checks.summarize("# Rent is due tomorrow\nPay it in the bank app.") == (
        "Rent is due tomorrow",
        "Pay it in the bank app.",
    )
    assert agent_checks.summarize("NOTHING") is None
    assert len(agent_checks.summarize("x" * 500)[0]) == agent_checks.MAX_TITLE


def test_the_ideas_pass_ignores_check_chats(store):
    from pi.schedule import proposals

    job = agent_checks.create_check(store, check(), now=NOON)
    session = agent_checks.session_for(store, job["id"])
    store.append_message(session, "user", "Look for exams and deadlines.")
    other = store.create_session(title="Plan")
    store.append_message(other, "user", "Remind me about judo.")
    with store._connect() as db:
        found = proposals._evidence(db, 0, 1e12)
    assert [row["session_id"] for row in found] == [other]


def test_forgetting_the_chat_clears_its_notes_and_starts_fresh(store):
    job = agent_checks.create_check(store, check(), now=NOON)
    session = agent_checks.session_for(store, job["id"])
    run = claimed_run(store, job)
    agent_checks.AgentChecks(store, Loop(reply("Bill due Friday.", session))).run(run["id"])
    assert agent_checks.notes(store)
    with store._connect() as db:
        agent_checks.redact(db, [session])
    assert agent_checks.notes(store) == [] and agent_checks.session_for(store, job["id"]) is None
