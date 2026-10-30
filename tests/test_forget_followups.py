"""Forgetting a chat undoes what it left elsewhere (2026-10-29 review, F-25, F-26).

Forgetting runs with Pi stopped, so it queues: the ToolGate approval requests its parked turns wait
on are withdrawn, the memories Conker kept from it on its own are forgotten in MemoryGate, and the
AI jobs it made on its own are turned off and marked (never deleted). Once Pi runs again the
follow-up worker does the rest; a failure is retried and shows in Pi's health, and never blocks
the forgetting.
"""

import json
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_tool_turns import FakeGate, build

from pi.actions import withdrawals
from pi.actions.toolgate import ToolGateClient, ToolGateUnavailable
from pi.agents import keep
from pi.conversation import forget_followups, forgetting
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.memory import followups as memory_followups
from pi.memory.corrections import CorrectionError
from pi.schedule import jobs
from pi.turns import native_tools

JOB = {"name": "Morning check", "instructions": "Check.", "repeat": "daily", "time": "08:00"}


def remembered(identity, text, origin="owner"):
    return {"memoryId": "mem_kept", "status": "remembered"}


@pytest.fixture
def forgotten(tmp_path):
    """A chat with a Keep memory, a Keep AI job and a parked turn, then forgotten offline."""
    path = tmp_path / "pi.db"
    store = Store(path)
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, ['{"tool": "t_echo", "args": {"text": "hi"}}'], gate)
    loop.remember = remembered
    session = store.create_session()
    execution = session_settings.execution(store, session)
    for tool_id, args in (
        (native_tools.REMEMBER, {"text": "The exam is Thursday."}),
        (native_tools.SCHEDULE, JOB),
    ):
        native_tools.call(
            loop, tool_id, args, session_id=session, turn_id=None, execution=execution, run=True
        )
    [job] = jobs.list_jobs(store)
    assert loop.run_turn(session, "Echo hi.")["status"] == "awaiting_approval"
    other = store.create_session()
    store.close()
    plan = forgetting.preview(path, session)
    assert plan["approval_request_ids"] == ["req_1"]
    forgetting.forget(path, session, plan["confirmation"])
    store = Store(path)
    yield store, session, job, other
    store.close()


def test_forgetting_queues_the_withdrawal_and_the_memory_and_turns_the_job_off(forgotten):
    store, session, job, _ = forgotten
    with store._connect() as db:
        [row] = db.execute("SELECT * FROM approval_withdrawals").fetchall()
    assert (row["request_id"], row["agent_id"], row["state"]) == ("req_1", "companion", "pending")
    rows = {row["kind"]: row for row in keep.listing(store, session)}
    assert rows["memory"]["undo"] == "pending" and rows["ai_job"]["undo"] == "done"
    off = jobs.get_job(store, job["id"])
    assert off["definition"]["enabled"] is False
    assert off["definition"]["turnedOff"] == "chat_forgotten"
    assert jobs.browser_job(off).definition.turnedOff == "chat_forgotten"
    assert forget_followups.status(store) == {
        "status": "ok",
        "approvalWithdrawals": 1,
        "memoryForgets": 1,
        "blocked": 0,
    }


class Gate:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []
        self.execution_key = "k"

    def withdraw(self, request_id, agent):
        self.calls.append((request_id, agent))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def state(store):
    with store._connect() as db:
        return db.execute("SELECT state, next_at FROM approval_withdrawals").fetchone()


def test_the_withdrawal_retries_until_toolgate_answers(forgotten):
    store, *_ = forgotten
    gate = Gate([ToolGateUnavailable("ConnectError"), ToolGateUnavailable("withdraw_401"), "gone"])
    assert withdrawals.drain(store, gate, now=1000.0) == 0
    pending = state(store)
    assert pending["state"] == "pending" and pending["next_at"] > 1000.0
    assert withdrawals.drain(store, gate, now=1001.0) == 0 and len(gate.calls) == 1  # waits
    assert withdrawals.drain(store, gate, now=pending["next_at"]) == 0
    assert state(store)["state"] == "blocked"
    assert forget_followups.status(store)["status"] == "degraded"
    # Blocked is still tried again, and a request the owner already answered counts as gone.
    assert withdrawals.drain(store, gate, now=10**10) == 1
    assert state(store)["state"] == "gone" and gate.calls[0] == ("req_1", "companion")


def test_no_toolgate_leaves_it_queued_and_says_so(forgotten):
    store, *_ = forgotten
    assert withdrawals.drain(store, None) == 0
    assert forget_followups.status(store, toolgate=False)["status"] == "degraded"


class Corrections:
    agent_id, destination = "owner", "d" * 64

    def __init__(self, revision=1, missing=False):
        self.revision, self.missing, self.forgotten = revision, missing, []

    def memory(self, identity):
        if self.missing:
            raise CorrectionError(404)
        return {"id": identity, "revision": self.revision, "text": "The exam is Thursday."}

    def forget(self, identity, memory_id, revision):
        self.forgotten.append((identity, memory_id, revision))
        return {
            "request_id": identity,
            "agent_id": self.agent_id,
            "memory_id": memory_id,
            "revision": revision,
            "status": "forgotten",
            "index_removal": "removed",
        }


def test_the_kept_memory_is_forgotten_once_pi_runs(forgotten):
    store, session, *_ = forgotten
    client = Corrections(revision=2)
    assert forget_followups.tick(store, Gate(["withdrawn"]), client) == 2
    [(identity, memory_id, revision)] = client.forgotten
    assert (memory_id, revision) == ("mem_kept", 2) and identity.startswith("keepforget-")
    rows = {row["kind"]: row for row in keep.listing(store, session)}
    assert rows["memory"]["undo"] == "done"
    with store._connect() as db:
        assert db.execute("SELECT state FROM memory_forget_operations").fetchone()[0] == "forgotten"
    assert forget_followups.status(store)["approvalWithdrawals"] == 0
    assert memory_followups.drain(store, client) == 0  # nothing left


def test_a_memory_already_gone_needs_nothing(forgotten):
    store, session, *_ = forgotten
    assert memory_followups.drain(store, Corrections(missing=True)) == 1
    assert {row["kind"]: row for row in keep.listing(store, session)}["memory"]["undo"] == "gone"


def test_what_the_owner_approved_stays(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        loop, _ = build(store, [], FakeGate())
        session = store.create_session()
        native_tools.call(
            loop,
            native_tools.SCHEDULE,
            JOB,
            session_id=session,
            turn_id=None,
            execution=session_settings.execution(store, session),
        )
        from pi.agents import change_requests

        [request] = change_requests.listing(store)
        job = change_requests.approve(store, request["id"])["result"]["jobId"]
    forgetting.forget(path, session, forgetting.preview(path, session)["confirmation"])
    with closing(Store(path)) as store:
        assert jobs.get_job(store, job)["definition"]["enabled"] is True


class Withdraw(BaseHTTPRequestHandler):
    answers: list[tuple[int, dict]] = []
    seen: list[tuple[str, str | None, str]] = []

    def do_POST(self):
        cls = type(self)
        cls.seen.append(
            (
                self.path,
                self.headers.get("X-ToolGate-Agent"),
                self.headers["X-ToolGate-Execution-Key"],
            )
        )
        code, body = cls.answers.pop(0)
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def toolgate():
    Withdraw.answers, Withdraw.seen = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Withdraw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield ToolGateClient(f"http://127.0.0.1:{server.server_port}", "exec-key")
    server.shutdown()
    server.server_close()


def test_the_client_withdraws_as_the_agent_that_asked(toolgate):
    Withdraw.answers = [(200, {"id": "req_1", "status": "withdrawn"})]
    assert toolgate.withdraw("req_1", "companion") == "withdrawn"
    assert Withdraw.seen == [("/v2/agent/requests/req_1/withdraw", "companion", "exec-key")]


def test_a_request_from_before_identities_is_withdrawn_as_the_key(toolgate):
    Withdraw.answers = [(404, {}), (200, {"status": "withdrawn"})]
    assert toolgate.withdraw("req_1", "companion") == "withdrawn"
    assert [agent for _, agent, _ in Withdraw.seen] == ["companion", None]


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([(409, {"detail": {"code": "REQUEST_NOT_PENDING"}})], "gone"),
        ([(404, {}), (404, {})], "gone"),
    ],
)
def test_answered_or_unknown_requests_are_gone(toolgate, answers, expected):
    Withdraw.answers = answers
    assert toolgate.withdraw("req_1", "companion") == expected


def test_a_failing_toolgate_is_reported_for_a_retry(toolgate):
    Withdraw.answers = [(500, {})]
    with pytest.raises(ToolGateUnavailable) as failed:
        toolgate.withdraw("req_1", "companion")
    assert failed.value.reason == "withdraw_500"
    assert toolgate.withdraw("../admin", "companion") == "gone" and len(Withdraw.seen) == 1


def test_pis_health_says_what_a_forgotten_chat_still_owes(forgotten, monkeypatch):
    from types import SimpleNamespace

    from pi.api import server as api

    store, *_ = forgotten
    state = {
        "store": SimpleNamespace(health=lambda: {"status": "ok"}),
        "loop": SimpleNamespace(memory=SimpleNamespace(health=lambda: {"status": "ok"})),
        "hosted": None,
        "provider_credentials": [],
        "speech": SimpleNamespace(
            capabilities=lambda: {"stt": {"status": "ok"}, "tts": {"status": "ok"}}
        ),
        "speech_credential_configured": False,
        "toolgate": None,
        "local": SimpleNamespace(health=lambda: {"status": "ok"}),
        # No ToolGate and no memory capability: what is queued cannot go, so it is reported.
        "forget_followups": forget_followups.Followups(store, None, None),
    }
    for key, value in state.items():
        monkeypatch.setattr(api.app.state, key, value, raising=False)
    monkeypatch.setattr(api, "_health_cache", {})
    check = api.health()["checks"]["forget_followups"]
    assert check["status"] == "degraded" and check["approvalWithdrawals"] == 1
    assert "req_1" not in json.dumps(check) and "mem_kept" not in json.dumps(check)
