"""A cron job waiting on the owner: cancelling takes its ToolGate request back, and an AI job whose
turn parks on an approval holds the next run back until he answers."""

from contextlib import closing
from types import SimpleNamespace

import pytest
from test_agent_jobs import NOON, Loop, ai_job, claimed_run
from test_jobs import definition

from pi.actions import withdrawals
from pi.agents import inbox
from pi.conversation.store import Store
from pi.schedule import agent_jobs, jobs
from pi.turns import answers


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "job-waits.db")) as store:
        yield store


class Gate:
    execution_key = "k"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def withdraw(self, request_id, agent):
        self.calls.append((request_id, agent))
        return self.answer


def queued(store):
    with store._connect() as db:
        return [
            tuple(row)
            for row in db.execute("SELECT request_id, agent_id, state FROM approval_withdrawals")
        ]


def waiting_run(store, request_id="tg-request-1", key="cancel_waits_request"):
    job = jobs.create(store, definition(), now=0)
    run = jobs.run_now(store, job["id"], key, now=1)
    jobs.dispatch_claim(
        store, run, lambda *a, **kw: {"status": "awaiting_approval", "request_id": request_id}
    )
    return job, run


def test_cancelling_a_waiting_run_takes_its_toolgate_request_back(store):
    _, run = waiting_run(store)
    jobs.cancel_run(store, run["id"])
    jobs.cancel_run(store, run["id"])  # a repeat queues nothing more
    # Pi's own key asked (PublishedJobs), so the request is withdrawn as the key, not an agent.
    assert queued(store) == [("tg-request-1", None, "pending")]
    gate = Gate("withdrawn")
    assert withdrawals.drain(store, gate, now=10) == 1
    assert gate.calls == [("tg-request-1", None)]
    assert queued(store) == [("tg-request-1", None, "withdrawn")]


def test_cancelling_work_that_never_asked_queues_nothing(store):
    job = jobs.create(store, definition(), now=0)
    run = jobs.run_now(store, job["id"], "cancel_ready_request", now=1)
    jobs.cancel_run(store, run["id"])
    assert queued(store) == []


def test_a_rejected_request_cancels_its_run_and_needs_no_withdrawal(store):
    _, run = waiting_run(store, "tg-rejected")
    answers.decided(
        SimpleNamespace(store=store),
        "tg-rejected",
        "rejected",
        resume_run=lambda run_id: pytest.fail("a rejected run resumed"),
        cancel_run=lambda run_id: jobs.cancel_run(store, run_id),
    )
    assert jobs.runs(store, run["job_id"])[0]["status"] == "cancelled"
    # ToolGate already answered: the withdrawal finds it gone (409) and is settled.
    assert withdrawals.drain(store, Gate("gone"), now=10) == 1
    assert queued(store) == [("tg-rejected", None, "gone")]


def parked(store, job):
    """Run the AI job once with a turn that parks on an approval; return its run and turn."""
    session = agent_jobs.session_for(store, job["id"])
    turn = store.start_turn(session)
    with store._connect() as db:
        db.execute("UPDATE turns SET status='awaiting_approval' WHERE id=?", (turn,))
    run = claimed_run(store, job)
    loop = Loop({"session_id": session, "turn_id": turn, "status": "awaiting_approval"})
    assert jobs.dispatch_claim(store, run, agent_jobs.AgentJobs(store, loop)) == "completed"
    return run, turn


def test_a_parked_ai_job_holds_the_next_run_back_until_the_owner_answers(store):
    job = agent_jobs.create(store, ai_job(), now=NOON)
    run, turn = parked(store, job)
    assert jobs.runs(store, job["id"])[0]["receipt"]["waitingForApproval"] is True
    # The next slot is skipped (its chat is busy), not dispatched into a session_busy refusal.
    with store._connect() as db:
        db.execute("UPDATE scheduled_jobs SET next_at=? WHERE id=?", (NOON + 1, job["id"]))
    assert jobs.claim_due(store, now=NOON + 7200) == []
    with pytest.raises(jobs.JobError, match="Resolve the current run"):
        jobs.run_now(store, job["id"], "while_it_waits_request", now=NOON + 7201)
    # Not yet handled: the Inbox neither lists nor counts it while the turn waits.
    day = inbox.inbox(store, now=NOON + 60)
    assert run["id"] not in str(day["handled"])
    assert day["impact"]["today"]["jobsCompleted"] == 0
    # Approving (or refusing) ends the turn; then the job runs again and counts as handled.
    with store._connect() as db:
        db.execute("UPDATE turns SET status='complete' WHERE id=?", (turn,))
    day = inbox.inbox(store, now=NOON + 60)
    assert run["id"] in str(day["handled"])
    assert day["impact"]["today"]["jobsCompleted"] == 1
    assert jobs.run_now(store, job["id"], "after_answer_request", now=NOON + 7300)["id"]


def test_reconciling_a_parked_ai_job_keeps_it_waiting(store, monkeypatch):
    saved = {"effective_session_id": "ses_x", "turn_id": "trn_x", "status": "awaiting_approval"}
    monkeypatch.setattr(agent_jobs.submissions, "get", lambda *a: saved)
    adapter = agent_jobs.AgentJobs(store, Loop())
    outcome = adapter.reconcile({"kind": "agent"}, action_id="scheduled_x", agent_id="companion")
    assert outcome == {
        "status": "completed",
        "sessionId": "ses_x",
        "turnId": "trn_x",
        "waitingForApproval": True,
    }
    saved["status"] = "complete"
    assert "waitingForApproval" not in adapter.reconcile(
        {"kind": "agent"}, action_id="scheduled_x", agent_id="companion"
    )
