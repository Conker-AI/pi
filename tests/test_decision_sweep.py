"""A decision made in ToolGate that Pi was not told about (its admin key, or Pi down) still
continues the work: Pi reads the status with its own key and takes the owner route's step."""

from contextlib import closing

import httpx
import pytest
from test_inbox_answers import inputs, parked
from test_jobs import definition

from pi.actions.toolgate import ToolGateClient
from pi.conversation.store import Store
from pi.schedule import jobs
from pi.turns import answers, decision_sweep


@pytest.fixture()
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        yield store


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    monkeypatch.setattr(answers, "background", lambda work: work())


class Statuses:
    """ToolGate's answer per request, as Pi's key reads it."""

    execution_key = "k"

    def __init__(self, **statuses):
        self.statuses, self.reads = statuses, []

    def request_status(self, request_id, agent):
        self.reads.append((request_id, agent))
        return self.statuses.get(request_id, "pending")


def test_an_admin_approval_resumes_the_parked_chat_turn_once(store):
    loop, gate, _, turn, request = parked(store, ["Echoed."])
    toolgate = Statuses(**{request: "approved"})
    assert decision_sweep.sweep(store, toolgate, loop) == 1
    assert toolgate.reads == [(request, "companion")]
    assert store.get_turn(turn)["status"] == "complete"
    assert gate.invocations[-1] == ("t_echo", {"text": "hi"}, request)
    # Nothing waits any more: the next pass reads nothing and runs nothing twice.
    assert decision_sweep.sweep(store, toolgate, loop) == 0
    assert len(gate.invocations) == 2


def test_an_admin_no_closes_the_turn_and_never_passes_a_note_as_his_words(store):
    loop, gate, session, turn, request = parked(store, ["Hello again."])
    before = inputs(store, session)
    assert decision_sweep.sweep(store, Statuses(**{request: "rejected"}), loop) == 1
    saved = store.get_turn(turn)
    assert (saved["status"], saved["detail"]) == ("cancelled", answers.CLOSED)
    assert len(gate.invocations) == 1
    assert inputs(store, session) == before


@pytest.mark.parametrize("status", ["pending", "withdrawn", None])
def test_an_undecided_or_unreadable_request_keeps_waiting(store, status):
    loop, gate, _, turn, request = parked(store, ["Echoed."])
    assert decision_sweep.sweep(store, Statuses(**{request: status}), loop) == 0
    assert store.get_turn(turn)["status"] == "awaiting_approval"
    assert len(gate.invocations) == 1


def test_no_toolgate_key_reads_nothing(store):
    loop, _, _, _, request = parked(store, ["Echoed."])
    toolgate = Statuses(**{request: "approved"})
    toolgate.execution_key = ""
    assert decision_sweep.sweep(store, toolgate, loop) == 0
    assert decision_sweep.sweep(store, None, loop) == 0
    assert toolgate.reads == []


def waiting_run(store, request_id):
    job = jobs.create(store, definition(), now=0)
    run = jobs.run_now(store, job["id"], "decision_sweep_" + request_id, now=1)
    jobs.dispatch_claim(
        store, run, lambda *a, **kw: {"status": "awaiting_approval", "request_id": request_id}
    )
    return run


def test_an_admin_decision_resumes_or_cancels_a_tool_cron_run(store):
    approved, refused = waiting_run(store, "tg-yes"), waiting_run(store, "tg-no")
    calls = []

    def executor(target, **kwargs):
        calls.append(kwargs)
        return {"status": "completed"}

    toolgate = Statuses(**{"tg-yes": "approved", "tg-no": "rejected"})
    loop = type("Loop", (), {"store": store})()
    assert decision_sweep.sweep(store, toolgate, loop, executor) == 2
    # Pi's own key asked for a cron job, so it reads as the key.
    assert sorted(toolgate.reads) == [("tg-no", None), ("tg-yes", None)]
    assert jobs.get_run(store, approved["id"])["status"] == "completed"
    assert calls == [
        {"action_id": approved["id"], "agent_id": "companion", "approval_request_id": "tg-yes"}
    ]
    assert jobs.get_run(store, refused["id"])["status"] == "cancelled"


class Response:
    def __init__(self, code, body=None):
        self.status_code, self.body = code, body

    def json(self):
        return self.body


def test_the_client_reads_as_the_agent_then_as_the_key(monkeypatch):
    client = ToolGateClient("http://toolgate.test", "k")
    seen = []

    def get(url, headers, **kwargs):
        seen.append(headers.get("X-ToolGate-Agent"))
        if len(seen) == 1:
            return Response(404)
        return Response(200, {"status": "approved", "decision": {"note": "his words"}})

    monkeypatch.setattr(httpx, "get", get)
    assert client.request_status("req-1", "helper") == "approved"
    assert seen == ["helper", None]
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: Response(404))
    assert client.request_status("req-1", "helper") is None
    assert client.request_status("bad id/../x", "helper") is None

    def down(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", down)
    assert client.request_status("req-1", None) is None
