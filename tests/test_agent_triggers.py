"""Tool triggers: an AI job starts when a watched get tool has something new, and only then."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

import pytest
from pydantic import ValidationError

from pi.conversation.store import Store
from pi.schedule import agent_jobs, agent_triggers, job_models, jobs

NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()
NIGHT = datetime.fromisoformat("2026-10-26T00:00:00+00:00").timestamp()
TIMING = dict(kind="interval", time="00:00", day=0, hours=24)
WATCH = dict(toolId="mcp.telegram.messages", args={"chat": "study group"}, everyMinutes=5)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "triggers.db")) as store:
        yield store


@dataclass
class Result:
    ok: bool
    result: object
    tool_id: str = "mcp.telegram.messages"


@dataclass
class Approval:
    request_id: str = "apr_1"


class Client:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def invoke(self, tool_id, args, **kwargs):
        self.calls.append((tool_id, args, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Published:
    def __init__(self, client):
        self.clients = {"companion": client}


def watched(store, **watch):
    body = job_models.AgentJobCreate.model_validate(
        dict(
            name="Study group",
            instructions="Tell me what the group needs from me.",
            timing=TIMING,
            timeZone="Asia/Jerusalem",
            watch={**WATCH, **watch},
        )
    )
    return agent_jobs.create(store, body, now=NOON)


def look(store, client, now):
    return agent_jobs.AgentJobs(store, loop=None, published=Published(client)).watch(now)


def runs(store, job):
    return jobs.runs(store, job["id"])


def test_only_ai_jobs_can_start_from_a_tool():
    with pytest.raises(ValidationError):
        job_models.Definition.model_validate(
            dict(
                name="x",
                instructions="y",
                agentId="companion",
                timing=TIMING,
                timeZone="UTC",
                enabled=True,
                target=dict(kind="tool", id="t", publishedVersion=1, digest="a" * 64, args={}),
                watch=WATCH,
            )
        )


def test_the_first_look_is_a_baseline_and_only_a_change_starts_the_job(store):
    job = watched(store)
    client = Client(Result(True, ["hi"]), Result(True, ["hi"]), Result(True, ["hi", "new message"]))
    assert look(store, client, NOON) == ["baseline"]
    assert look(store, client, NOON + 60) == []  # not due yet: every five minutes
    assert look(store, client, NOON + 301) == ["unchanged"]
    assert look(store, client, NOON + 602) == ["started"]
    [run] = runs(store, job)
    assert run["status"] == "ready" and agent_triggers.started_by_watch(run["request_id"])
    tool, args, kwargs = client.calls[0]
    assert tool == "mcp.telegram.messages" and args == {"chat": "study group"}
    assert kwargs["action_id"].startswith("watch_")


def test_a_change_while_a_run_is_going_waits_for_the_next_look(store):
    job = watched(store)
    client = Client(Result(True, [1]), Result(True, [2]), Result(True, [3]))
    look(store, client, NOON)
    assert look(store, client, NOON + 301) == ["started"]
    assert look(store, client, NOON + 602) == ["busy"]
    assert len(runs(store, job)) == 1


def test_a_tool_that_asks_each_time_stops_the_watch_once(store):
    job = watched(store)
    client = Client(Approval())
    assert look(store, client, NOON) == ["needs_approval"]
    assert look(store, client, NOON + 10_000) == []
    summary = jobs.browser_job(jobs.get_job(store, job["id"])).definition.watch
    assert summary.problem == "needs_approval" and summary.toolId == "mcp.telegram.messages"
    assert agent_triggers.FAILURES["needs_approval"].startswith("This tool asks for your OK")


def test_a_failed_look_is_retried_and_never_starts_the_job(store):
    job = watched(store)
    client = Client(RuntimeError("secret input text"), Result(True, ["a"]))
    assert look(store, client, NOON) == ["failed"]
    assert look(store, client, NOON + 301) == ["baseline"]
    assert runs(store, job) == []
    with store._connect() as db:
        stored = [tuple(row) for row in db.execute("SELECT * FROM agent_job_watch")]
    assert "secret" not in repr(stored) and "a" not in [value for row in stored for value in row]


def test_watches_pause_in_quiet_hours(store):
    watched(store)
    client = Client(Result(True, [1]))
    assert look(store, client, NIGHT) == ["paused"] and client.calls == []


def test_a_pause_keeps_the_last_failure(store):
    job = watched(store)
    client = Client(RuntimeError("down"))
    assert look(store, client, NOON) == ["failed"]
    assert look(store, client, NIGHT + 86_400) == ["paused"] and len(client.calls) == 1
    summary = jobs.browser_job(jobs.get_job(store, job["id"])).definition.watch
    assert summary.problem == "failed"


def test_a_watched_job_never_starts_from_the_clock(store):
    job = watched(store)
    with store._connect() as db:
        db.execute("UPDATE scheduled_jobs SET next_at=? WHERE id=?", (NOON - 1, job["id"]))
    assert jobs.claim_due(store, now=NOON) == []


def test_the_run_says_which_tool_has_something_new(store):
    job = watched(store)
    client = Client(Result(True, [1]), Result(True, [2]))
    look(store, client, NOON)
    look(store, client, NOON + 301)
    [run] = runs(store, job)

    class Loop:
        def run_turn(self, session_id, text, **kwargs):
            self.text = text
            return {
                "session_id": session_id,
                "turn_id": "trn_1",
                "message": {"id": "msg_" + "1" * 16, "content": "NOTHING"},
            }

    loop = Loop()
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='dispatching' WHERE id=?", (run["id"],))
    agent_jobs.AgentJobs(store, loop).run(run["id"])
    assert "mcp.telegram.messages has something new" in loop.text
    assert loop.text.endswith("Tell me what the group needs from me.")


def test_changing_the_watch_starts_over(store):
    job = watched(store)
    look(store, Client(Approval()), NOON)
    saved = jobs.get_job(store, job["id"])
    definition = job_models.Definition.model_validate(saved["definition"])
    definition.watch = job_models.ToolWatch(toolId="mcp.mail.inbox", everyMinutes=15)
    jobs.update(
        store,
        job["id"],
        job_models.Update(expected_revision=saved["revision"], definition=definition),
    )
    assert jobs.browser_job(jobs.get_job(store, job["id"])).definition.watch.problem is None
