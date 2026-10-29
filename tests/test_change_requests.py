"""Conker prepares cron jobs, tools and memories; nothing becomes real until the owner approves."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from test_tool_turns import FakeGate, build

from pi.agents import change_requests, tool_drafts
from pi.api import server as api
from pi.api.browser_contract import owner_allowed, session_only_write
from pi.conversation.store import Store
from pi.schedule import jobs
from pi.turns import native_tools

OWNER = "change-requests-owner-key-" + "o" * 32


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def schedule(store, **args):
    loop, _ = build(store, [], FakeGate())
    value = {
        "name": "Weekly life review",
        "instructions": "Look at my whole week and suggest a better plan.",
        "repeat": "weekly",
        "day": "sunday",
        "time": "21:00",
    }
    value.update(args)
    return native_tools.call(
        loop,
        native_tools.SCHEDULE,
        value,
        session_id=None,
        turn_id=None,
        execution={"kind": "companion", "agentId": "companion"},
    )


def test_scheduling_files_a_request_and_creates_nothing(store):
    result = schedule(store)
    assert result.ok and result.result["status"] == "waiting_for_owner"
    [request] = change_requests.listing(store)
    assert (
        request["kind"] == "ai_job"
        and request["title"] == "Create the AI job “Weekly life review”, every Sunday at 21:00"
    )
    assert request["payload"]["timing"] == {
        "kind": "weekly",
        "time": "21:00",
        "day": 0,
        "hours": 24,
    }
    assert jobs.list_jobs(store) == []


def test_approving_creates_exactly_what_was_filed_once(store):
    schedule(store, repeat="webhook")
    [request] = change_requests.listing(store)
    approved = change_requests.approve(store, request["id"])
    assert approved["state"] == "approved"
    [job] = jobs.list_jobs(store)
    assert job["id"] == approved["result"]["jobId"]
    assert job["definition"]["name"] == "Weekly life review" and job["definition"]["target"] == {
        "kind": "agent",
        "clock": False,
    }
    with pytest.raises(change_requests.ChangeRequestError) as again:
        change_requests.approve(store, request["id"])
    assert again.value.status == 409
    assert len(jobs.list_jobs(store)) == 1


def test_declining_creates_nothing(store):
    schedule(store)
    [request] = change_requests.listing(store)
    assert change_requests.decline(store, request["id"])["state"] == "declined"
    assert change_requests.listing(store) == [] and jobs.list_jobs(store) == []


@pytest.mark.parametrize(
    "args",
    [
        {"repeat": "monthly"},
        {"repeat": "hours", "hours": 99},
        {"time": "25:00"},
        {"day": "someday"},
        {"name": ""},
    ],
)
def test_bad_arguments_are_an_answer_not_a_request(store, args):
    result = schedule(store, **args)
    assert not result.ok and result.result["status"] == "invalid"
    assert change_requests.listing(store) == []


def test_a_tool_draft_waits_for_the_owner_to_save_it(store):
    loop, provider = build(store, [], FakeGate())
    result = native_tools.call(
        loop,
        native_tools.DRAFT_TOOL,
        {"description": "Summarize my study group chat"},
        session_id=None,
        turn_id=None,
        execution={"kind": "companion", "agentId": "companion"},
    )
    # Filed at once, drafted later in the background: no model was asked in the turn.
    assert result.ok and result.result["status"] == "drafting" and provider.sent == []
    [request] = change_requests.listing(store)
    assert request["kind"] == "tool_draft"
    assert request["payload"] == {
        "description": "Summarize my study group chat",
        "status": "drafting",
    }
    assert result.result["requestId"] == request["id"]
    document = {"name": "Group digest", "nodes": [], "edges": []}
    drafted = {**request["payload"], "status": "ready", "document": document, "issues": []}
    assert tool_drafts.finish(store, request, drafted)
    with pytest.raises(change_requests.ChangeRequestError) as unsaved:
        change_requests.approve(store, request["id"])
    assert unsaved.value.code == "draft_required"
    done = change_requests.saved(
        store, request["id"], change_requests.DraftSaved(draftId="group-digest")
    )
    assert done["result"] == {"draftId": "group-digest"}


def test_a_tool_draft_needs_a_description(store):
    loop, _ = build(store, [], FakeGate())
    result = native_tools.call(
        loop,
        native_tools.DRAFT_TOOL,
        {"description": "  "},
        session_id=None,
        turn_id=None,
        execution={"kind": "companion", "agentId": "companion"},
    )
    assert result.result["status"] == "invalid" and change_requests.listing(store) == []


def test_remembering_files_a_request_and_saves_only_on_approval(store):
    loop, _ = build(store, [], FakeGate())
    native_tools.call(
        loop,
        native_tools.REMEMBER,
        {"text": "The owner's physics exam is on Thursday."},
        session_id=None,
        turn_id=None,
        execution=None,
    )
    [request] = change_requests.listing(store)
    with pytest.raises(change_requests.ChangeRequestError) as unavailable:
        change_requests.approve(store, request["id"])
    assert unavailable.value.status == 503

    def down(identity, text):
        raise OSError("memory is down")

    with pytest.raises(change_requests.ChangeRequestError) as failed:
        change_requests.approve(store, request["id"], memory=down)
    assert failed.value.status == 503
    assert change_requests.get(store, request["id"])["state"] == "open"  # the owner can retry
    saved = []
    done = change_requests.approve(
        store,
        request["id"],
        memory=lambda identity, text: saved.append((identity, text)) or {"memoryId": "m1"},
    )
    assert saved == [(request["id"], "The owner's physics exam is on Thursday.")]
    assert done["result"] == {"memoryId": "m1"}


def test_a_chat_turn_can_make_a_cron_job(store):
    # Scheduling is Keep: allowed by default, made at once and recorded as an approved request.
    call = '{"tool": "conker.schedule", "args": {"name": "Morning check", "instructions": "Tell me what needs me today.", "repeat": "daily", "time": "08:00"}}'
    loop, provider = build(store, [call, "It is set up."], FakeGate())
    session = store.create_session()
    result = loop.run_turn(session, "Every morning at 8, check what needs me.")
    assert result["message"]["content"] == "It is set up."
    assert change_requests.listing(store) == []
    [made] = change_requests.listing(store, state="approved")
    assert made["sessionId"] == session and made["payload"]["timing"]["kind"] == "daily"
    assert any("conker.schedule" in message.content for message in provider.sent[0])
    assert jobs.list_jobs(store)[0]["id"] == made["result"]["jobId"]


def test_other_agents_get_conkers_tools_only_when_selected():
    assert native_tools.catalog({"kind": "agent", "configuration": {"toolIds": []}}) == []
    chosen = native_tools.catalog(
        {"kind": "agent", "configuration": {"toolIds": ["conker.remember"]}}
    )
    assert [tool.id for tool in chosen] == ["conker.remember"]


def test_the_owner_routes_and_contract(store, monkeypatch):
    request = "/change-requests/creq_" + "a" * 32
    assert owner_allowed("GET", "/change-requests") and owner_allowed("POST", request + "/approve")
    assert session_only_write("POST", request + "/decline") and not session_only_write(
        "POST", request + "/approve"
    )
    assert owner_allowed("POST", request + "/saved") and session_only_write(
        "POST", request + "/saved"
    )
    schedule(store)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(api.app.state, "toolgate", None, raising=False)
    client = TestClient(api.app)
    owner = {"X-Pi-Owner-Key": OWNER}
    [filed] = client.get("/change-requests", headers=owner).json()["results"]
    # Reporting a saved draft never makes a job: only the verified approval does.
    wrong = client.post(
        f"/change-requests/{filed['id']}/saved", headers=owner, json={"draftId": "x"}
    )
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "not_a_tool_draft"
    approved = client.post(f"/change-requests/{filed['id']}/approve", headers=owner)
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    listed = client.get("/tools", headers={"X-Pi-Key": "synthetic-admin"}).json()
    assert {"conker.schedule", "conker.draft_tool", "conker.remember"} <= {
        tool["id"] for tool in listed["results"]
    }


def test_forgetting_the_chat_drops_its_requests(store):
    session = store.create_session()
    change_requests.file(store, "memory", "Remember: x", "x", {"text": "x"}, session_id=session)
    with store._connect() as db:
        change_requests.redact(db, [session])
    assert change_requests.listing(store) == []


def test_the_memory_client_stores_a_statement_by_request_id():
    import httpx

    from pi.memory import corrections

    seen = []

    def answer(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"agent_id": "owner", "memory_id": "m1", "status": "remembered", "revision": 1},
        )

    client = corrections.Client(
        "http://memory.test", "k" * 32, "owner", transport=httpx.MockTransport(answer)
    )
    identity = "creq_" + "c" * 32
    assert client.remember(identity, "Exam on Thursday.") == {
        "memoryId": "m1",
        "status": "remembered",
    }
    assert seen == [
        ("PUT", f"/runtime/corrections/remember/{identity}", {"text": "Exam on Thursday."})
    ]


@pytest.mark.parametrize(
    ("payload", "path", "body"),
    [
        ({"operation": "approve", "id": "creq_" + "d" * 32}, "/approve", {}),
        ({"operation": "decline", "id": "creq_" + "d" * 32}, "/decline", {}),
        (
            {"operation": "saved", "id": "creq_" + "d" * 32, "draftId": "digest"},
            "/saved",
            {"draftId": "digest"},
        ),
    ],
)
def test_the_cli_decides_requests_through_the_owner_routes(payload, path, body):
    import io

    import httpx

    from gateway.mutation import apply_resource
    from tests.test_gateway_mutation import config

    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"id": payload["id"], "state": "approved"})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        apply_resource(config(), "requests", io.BytesIO(json.dumps(payload).encode()), client)
    [request] = seen
    assert request.url.path == "/change-requests/" + payload["id"] + path
    assert owner_allowed("POST", request.url.path) and json.loads(request.content) == body
    assert request.headers["x-pi-owner-key"] == config().pi_owner_key


def test_the_cli_never_edits_a_request():
    import io

    from gateway.mutation import MutationError, apply_resource
    from tests.test_gateway_mutation import config

    for payload in (
        {"operation": "approve", "id": "creq_" + "d" * 32, "payload": {"name": "x"}},
        {"operation": "edit", "id": "creq_" + "d" * 32},
    ):
        with pytest.raises(MutationError):
            apply_resource(config(), "requests", io.BytesIO(json.dumps(payload).encode()), None)


def test_a_request_mixed_with_text_gets_one_more_try(store):
    call = '{"tool": "conker.remember", "args": {"text": "The exam is on Thursday."}}'
    loop, provider = build(store, ["Sure!\n" + call, call, "Approve it in your Inbox."], FakeGate())
    result = loop.run_turn(store.create_session(), "Remember that the exam is on Thursday.")
    assert result["message"]["content"] == "Approve it in your Inbox."
    [request] = change_requests.listing(store)
    assert request["payload"] == {"text": "The exam is on Thursday."}
    retry = [m.content for m in provider.sent[1] if m.role == "system"][-1]
    assert "mixed a tool request" in retry and "Sure!" not in json.dumps(
        [m.content for m in provider.sent[1]]
    )


def test_a_second_mixed_reply_still_runs_nothing(store):
    from pi.turns.failures import TurnFailed

    call = 'Okay\n{"tool": "conker.remember", "args": {"text": "x"}}'
    loop, _ = build(store, [call, call], FakeGate())
    with pytest.raises(TurnFailed):
        loop.run_turn(store.create_session(), "Remember x.")
    assert change_requests.listing(store) == []


def test_the_owner_adds_a_memory_by_hand_through_the_same_route(monkeypatch):
    import io

    import httpx

    from gateway.mutation import apply_resource
    from pi.memory import stated
    from tests.test_gateway_mutation import config

    calls = []

    class Memory:
        def remember(self, identity, text):
            calls.append((identity, text))
            return {"memoryId": "m1", "status": "remembered"}

    body = stated.Remember(request_id="r" * 16, text="My exam is on Thursday.")
    assert stated.remember(Memory(), body) == {"memoryId": "m1", "status": "remembered"}
    assert calls == [("r" * 16, "My exam is on Thursday.")]
    with pytest.raises(stated.RememberError) as missing:
        stated.remember(None, body)
    assert missing.value.status == 503
    for text in ("", "   ", "a\x00b", "x" * 2001):
        with pytest.raises(ValueError):
            stated.Remember(request_id="r" * 16, text=text)
    assert owner_allowed("POST", "/memory/remember")
    assert not session_only_write("POST", "/memory/remember")  # asks for the password

    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"memoryId": "m1", "status": "remembered"})

    payload = {"operation": "remember", "request_id": "r" * 16, "text": "My exam is on Thursday."}
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        apply_resource(config(), "memory", io.BytesIO(json.dumps(payload).encode()), client)
    [request] = seen
    assert request.url.path == "/memory/remember"
    assert json.loads(request.content) == {
        "request_id": "r" * 16,
        "text": "My exam is on Thursday.",
    }
