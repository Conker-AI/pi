"""Forgetting a chat asks ToolGate to forget its tool records too (forgetting gap 9).

ToolGate keeps each approval request's and action's arguments and results. Forgetting runs with
Pi stopped, so it queues their ids; once Pi runs again the follow-up worker sends them to
ToolGate's /v2/agent/forget as the agent that made them, after the withdrawals.
"""

import json
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_tool_turns import FakeGate, build

from pi.actions import tool_forgets
from pi.actions.toolgate import ToolGateClient, ToolGateUnavailable
from pi.conversation import forget_followups, forgetting
from pi.conversation.store import Store

CALL = '{"tool": "t_echo", "args": {"text": "hi"}}'


def nothing(**extra):
    return {"forgotten": [], "already": [], "unknown": [], "refused": [], **extra}


def every(request_ids, action_ids):
    return {
        "requests": nothing(forgotten=list(request_ids)),
        "actions": nothing(forgotten=list(action_ids)),
    }


def unknown(request_ids, action_ids):
    return {
        "requests": nothing(unknown=list(request_ids)),
        "actions": nothing(unknown=list(action_ids)),
    }


class Gate:
    """ToolGate's forget route answered from a script; `withdraw` for the withdrawal drain."""

    execution_key = "k"

    def __init__(self, *answers):
        self.answers, self.calls, self.withdrawn = list(answers), [], []

    def forget_records(self, request_ids, action_ids, agent):
        self.calls.append((list(request_ids), list(action_ids), agent))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer(request_ids, action_ids) if callable(answer) else answer

    def withdraw(self, request_id, agent):
        self.withdrawn.append(request_id)
        return "withdrawn"


@pytest.fixture
def chats(tmp_path):
    """Two chats with one tool call each, the first one forgotten. With `answered` its approval
    was answered; without, its turn still waits on it."""

    def make(answered):
        path = tmp_path / "pi.db"
        with closing(Store(path)) as store:
            loop, _ = build(store, [CALL, CALL], FakeGate(needs_approval=True))
            session, other = store.create_session(), store.create_session()
            loop.run_turn(session, "Echo hi.")
            loop.run_turn(other, "Echo hi.")
            with store._connect() as db:
                if answered:
                    db.execute("UPDATE turns SET status='complete' WHERE session_id=?", (session,))
                [action] = [
                    row[0]
                    for row in db.execute(
                        "SELECT a.id FROM tool_actions a JOIN turns t ON t.id=a.turn_id "
                        "WHERE t.session_id=?",
                        (session,),
                    )
                ]
                [request] = [
                    row[0]
                    for row in db.execute(
                        "SELECT approval_request_id FROM turns WHERE session_id=?", (session,)
                    )
                ]
        plan = forgetting.preview(path, session)
        assert plan["tool_record_ids"] == {"request": [request], "action": [action]}
        forgetting.forget(path, session, plan["confirmation"])
        return Store(path), request, action

    return make


def queued(store):
    with store._connect() as db:
        return {
            (row["kind"], row["id"]): row["state"]
            for row in db.execute("SELECT * FROM toolgate_forgets")
        }


def test_an_answered_approval_and_its_action_are_queued_sent_and_forgotten(chats):
    store, request, action = chats(answered=True)
    with closing(store):
        # Only the forgotten chat's records; the other chat's stay.
        assert queued(store) == {("request", request): "pending", ("action", action): "pending"}
        with store._connect() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(toolgate_forgets)")]
            assert not db.execute("SELECT 1 FROM approval_withdrawals").fetchone()
        # Ids only: nothing the chat said is kept here.
        assert columns == [
            "kind",
            "id",
            "agent_id",
            "state",
            "attempts",
            "next_at",
            "error",
            "created_at",
        ]
        assert forget_followups.status(store)["toolRecordForgets"] == 2
        gate = Gate(every)
        assert forget_followups.tick(store, gate, None, now=1000.0) == 2
        assert gate.calls == [([request], [action], "companion")]
        assert set(queued(store).values()) == {"forgotten"}
        assert forget_followups.status(store)["toolRecordForgets"] == 0


def test_a_waiting_request_is_forgotten_after_its_withdrawal(chats):
    store, request, _ = chats(answered=False)
    with closing(store):

        def until_withdrawn(request_ids, action_ids):
            waiting = request in request_ids and request not in gate.withdrawn
            return {
                "requests": nothing(refused=[{"id": request, "code": "still_pending"}])
                if waiting
                else nothing(forgotten=list(request_ids)),
                "actions": nothing(forgotten=list(action_ids)),
            }

        gate = Gate(until_withdrawn)
        # The worker withdraws first, so the same pass forgets the request.
        assert forget_followups.tick(store, gate, None, now=1000.0) == 3
        assert gate.withdrawn == [request] and set(queued(store).values()) == {"forgotten"}


def test_still_pending_or_running_is_retried_later(chats):
    store, request, action = chats(answered=True)
    with closing(store):
        busy = {
            "requests": nothing(refused=[{"id": request, "code": "still_pending"}]),
            "actions": nothing(refused=[{"id": action, "code": "still_running"}]),
        }
        gate = Gate(busy, every)
        assert tool_forgets.drain(store, gate, now=1000.0) == 0
        assert set(queued(store).values()) == {"pending"}
        assert tool_forgets.drain(store, gate, now=1001.0) == 0 and len(gate.calls) == 1  # waits
        assert tool_forgets.drain(store, gate, now=10**10) == 2
        assert set(queued(store).values()) == {"forgotten"}


def test_unknown_as_the_agent_is_asked_as_the_key_then_gone(chats):
    store, *_ = chats(answered=True)
    with closing(store):
        gate = Gate(unknown, unknown)
        assert tool_forgets.drain(store, gate, now=1000.0) == 2
        assert [agent for *_, agent in gate.calls] == ["companion", None]
        assert set(queued(store).values()) == {"gone"}


def test_a_refused_key_blocks_and_is_reported_then_retried(chats):
    store, request, action = chats(answered=True)
    with closing(store):
        refused = ToolGateUnavailable("forget_403")
        gate = Gate(refused, refused, ToolGateUnavailable("forget_409"), every)
        tool_forgets.drain(store, gate, now=1000.0)
        # Refused as the agent, then as the key itself: only then is it blocked.
        assert [agent for *_, agent in gate.calls] == ["companion", None]
        assert set(queued(store).values()) == {"blocked"}
        health = forget_followups.status(store)
        assert health["status"] == "degraded" and health["blocked"] == 2
        assert request not in json.dumps(health) and action not in json.dumps(health)
        # Recovery held in ToolGate (409) is only a wait; then it goes through.
        tool_forgets.drain(store, gate, now=10**9)
        assert set(queued(store).values()) == {"pending"}
        assert tool_forgets.drain(store, gate, now=10**10) == 2


def test_an_agent_toolgate_refuses_is_asked_as_the_key(chats):
    store, request, action = chats(answered=True)
    with closing(store):
        gate = Gate(ToolGateUnavailable("forget_403"), every)
        assert tool_forgets.drain(store, gate, now=1000.0) == 2
        assert gate.calls == [([request], [action], "companion"), ([request], [action], None)]
        assert set(queued(store).values()) == {"forgotten"}


def test_every_request_a_turn_parked_on_is_forgotten(tmp_path):
    """A turn that parks again keeps only the latest request id; the earlier one still holds
    the tool's arguments in ToolGate, so it is queued too."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        loop, _ = build(store, [CALL], FakeGate(needs_approval=True))
        session = store.create_session()
        loop.run_turn(session, "Echo hi.")
        with store._connect() as db:
            turn, first = db.execute(
                "SELECT id, approval_request_id FROM turns WHERE session_id=?", (session,)
            ).fetchone()
            db.execute("UPDATE turns SET status='running' WHERE id=?", (turn,))
        assert store.finish_turn(turn, "awaiting_approval", approval_request_id="req_again")
    plan = forgetting.preview(path, session)
    assert plan["tool_record_ids"]["request"] == sorted([first, "req_again"])
    forgetting.forget(path, session, plan["confirmation"])
    with closing(Store(path)) as store:
        assert {key for key in queued(store) if key[0] == "request"} == {
            ("request", first),
            ("request", "req_again"),
        }


def test_a_toolgate_without_the_route_is_retried_never_forgotten(chats):
    store, *_ = chats(answered=True)
    with closing(store):
        gate = Gate(ToolGateUnavailable("forget_404"))
        assert tool_forgets.drain(store, gate, now=1000.0) == 0
        assert set(queued(store).values()) == {"pending"}
        assert forget_followups.status(store)["toolRecordForgets"] == 2


def test_no_toolgate_leaves_it_queued_and_says_so(chats):
    store, *_ = chats(answered=True)
    with closing(store):
        assert tool_forgets.drain(store, None) == 0
        assert forget_followups.status(store, toolgate=False)["status"] == "degraded"


class Forget(BaseHTTPRequestHandler):
    answers: list[tuple[int, object]] = []
    seen: list[tuple[str, str | None, dict]] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, self.headers.get("X-ToolGate-Agent"), body))
        code, answer = type(self).answers.pop(0)
        payload = json.dumps(answer).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def toolgate():
    Forget.answers, Forget.seen = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Forget)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield ToolGateClient(f"http://127.0.0.1:{server.server_port}", "exec-key")
    server.shutdown()
    server.server_close()


def test_the_client_sends_the_ids_as_the_agent(toolgate):
    answer = {"requests": nothing(already=["req_1"]), "actions": nothing(forgotten=["pi_a"])}
    Forget.answers = [(200, answer)]
    assert toolgate.forget_records(["req_1"], ["pi_a"], "companion") == answer
    assert Forget.seen == [
        ("/v2/agent/forget", "companion", {"request_ids": ["req_1"], "action_ids": ["pi_a"]})
    ]


@pytest.mark.parametrize(
    ("code", "answer", "reason"),
    [
        (409, {"detail": {"code": "RECOVERY_HELD"}}, "forget_409"),
        # A ToolGate from before the route: not done yet, never forgotten.
        (404, {"detail": "Not Found"}, "forget_404"),
        (403, {}, "forget_403"),
        (200, {"requests": nothing()}, "forget_shape"),
        (200, {"requests": nothing(forgotten=[1]), "actions": nothing()}, "forget_shape"),
        (200, ["not", "a", "dict"], "forget_shape"),
    ],
)
def test_any_other_answer_is_a_retry(toolgate, code, answer, reason):
    Forget.answers = [(code, answer)]
    with pytest.raises(ToolGateUnavailable) as failed:
        toolgate.forget_records(["req_1"], [], None)
    assert failed.value.reason == reason
