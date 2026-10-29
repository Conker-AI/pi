"""Conker's tool requests are filed at once and drafted in the background, checked and repaired."""

import json
import threading
import time

import pytest

from pi.actions.toolgate import Tool, ToolGateUnavailable
from pi.agents import change_requests, draft_capabilities, tool_drafts
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.turns import inference_control, tool_drafting
from pi.turns.loop import Loop
from tests.test_model_roles import config
from tests.test_tool_turns import Scripted

DIGEST = "c" * 64
TOOLS = [
    Tool("web.search", "Web search", "Search the web for a topic", [{"name": "query"}], "auto"),
    Tool("web.fetch", "Fetch page", "Fetch one web page", [{"name": "url"}], "auto"),
    Tool("files.list", "List files", "List the files in a folder", [{"name": "folder"}]),
    Tool(f"workflow:daily-brief:3:{DIGEST}", "Daily brief", "A short brief of my day", []),
    Tool("conker.remember", "Remember", "Pi's own tool", [{"name": "text"}]),
]
DOCUMENT = {
    "name": "Topic reader",
    "description": "Searches the web for a topic and reads the first result.",
    "inputs": [{"name": "topic", "type": "string", "required": True}],
    "outputs": [],
    "nodes": [
        {"id": "start", "type": "input", "label": "Start", "config": {}},
        {
            "id": "search",
            "type": "tool_call",
            "label": "Search the web for the topic",
            "config": {"tool": "web.search", "args": {"query": "$input.topic"}},
        },
        {
            "id": "fetch",
            "type": "tool_call",
            "label": "Fetch the first result",
            "config": {"tool": "web.fetch", "args": {"url": "$steps.search.first"}},
        },
        {"id": "done", "type": "return", "label": "Done", "config": {"value": "$last"}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "search"},
        {"id": "e2", "source": "search", "target": "fetch"},
        {"id": "e3", "source": "fetch", "target": "done"},
    ],
    "effect": "read",
}
READY = json.dumps(
    {"reply": "Reads the first result for a topic.", "ready": True, "document": DOCUMENT}
)
WANTED = "Search the web for a topic and fetch the first result"


class Drafter:
    """The drafting model: a script of replies, or a function of the call."""

    name = "drafter"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((messages, timeout))
        reply = self.replies.pop(0) if self.replies else READY
        text = reply() if callable(reply) else reply
        return Completion(text=text, model=model, provider="drafter")


class Gate:
    def __init__(self, checks=None, tools=TOOLS):
        self.checks, self.checked, self._tools = list(checks or []), [], tools

    def tools(self):
        if self._tools is None:
            raise ToolGateUnavailable("ConnectError")
        return list(self._tools)

    def check_document(self, document):
        self.checked.append(document)
        found = self.checks.pop(0) if self.checks else {"valid": True, "issues": []}
        if isinstance(found, Exception):
            raise found
        return found


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def configure(store, **drafting):
    settings = config()
    if drafting:
        settings["roleSettings"]["roles"]["drafting"] = {
            **settings["roleSettings"]["roles"]["drafting"],
            **drafting,
        }
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(settings)
        ),
    )


def build(store, replies, gate=None, *, configured=True, **drafting):
    if configured:
        configure(store, **drafting)
    drafter = Drafter(replies)
    router = Router(
        local_provider=Scripted([]), local_model="test-model", providers={"one": drafter}
    )
    return Loop(store, router, toolgate=gate if gate is not None else Gate()), drafter


def shown(drafter, call=0):
    """What the drafting model was given on one call."""
    return json.loads(drafter.calls[call][0][-1].content)


def test_a_chat_turn_files_the_request_and_answers_without_waiting(store):
    call = json.dumps({"tool": "conker.draft_tool", "args": {"description": WANTED}})
    provider = Scripted([call, "I'm drafting it; it will be in your Inbox."])
    gate = Gate(tools=TOOLS[:3])
    loop = Loop(store, Router(local_provider=provider, local_model="m"), toolgate=gate)
    session = store.create_session()
    result = loop.run_turn(session, "Make me a tool that " + WANTED.lower())
    assert result["message"]["content"] == "I'm drafting it; it will be in your Inbox."
    # Two model calls only: the request and the answer. No reviewer, no drafting in the turn.
    assert len(provider.sent) == 2
    [request] = change_requests.listing(store)
    assert request["kind"] == "tool_draft" and request["sessionId"] == session
    assert request["payload"] == {"description": WANTED, "status": "drafting"}
    assert request["title"] == "Draft a tool: " + WANTED
    told = [m.content for m in provider.sent[1] if m.role == "tool"][-1]
    assert '"status": "drafting"' in told and "Inbox" in told
    assert loop.drafts._wake.is_set()  # the worker was woken


def test_the_tool_says_when_to_use_it():
    from pi.turns import native_tools

    [tool] = [t for t in native_tools.CATALOG if t.id == native_tools.DRAFT_TOOL]
    assert "whenever the owner asks for a new tool" in tool.description
    assert "instead of doing the job yourself" in tool.description


def test_the_worker_drafts_a_ready_card_with_plain_steps(store):
    gate = Gate()
    loop, drafter = build(store, [READY], gate)
    filed = tool_drafts.file(store, WANTED)
    assert loop.drafts.tick() == 1
    request = change_requests.get(store, filed["id"])
    payload = request["payload"]
    assert payload["status"] == "ready" and payload["checked"] is True and "note" not in payload
    assert payload["description"] == WANTED and payload["issues"] == []
    assert payload["document"]["id"] == "topic-reader" and payload["document"]["kind"] == "workflow"
    assert payload["steps"] == [
        "Start with the topic",
        "Search the web for the topic (uses Web search)",
        "Fetch the first result (uses Fetch page)",
        "Give back the previous step's result",
    ]
    assert request["state"] == "open" and request["title"] == "Add the tool “Topic reader”"
    assert request["summary"] == "Reads the first result for a topic."
    assert gate.checked == [payload["document"]]
    # Each model call gets the role's own limit (1 s in these settings), not ten minutes.
    assert [timeout for _, timeout in drafter.calls] == [1.0]
    assert tool_drafts.pending(store) == []


def test_only_matching_tools_are_offered_and_workflows_are_building_blocks(store):
    loop, drafter = build(store, [READY])
    tool_drafts.file(store, WANTED + ", then add my daily brief")
    loop.drafts.tick()
    offered = {item["id"]: item for item in shown(drafter)["capabilities"]}
    assert set(offered) == {"web.search", "web.fetch", "daily-brief"}
    assert offered["daily-brief"]["kind"] == "workflow" and offered["daily-brief"]["version"] == 3
    assert "authorization" not in offered["web.search"]


def test_the_drafting_role_drafts_and_the_answer_role_is_the_fallback(store):
    # Model b's provider is not connected here, so the drafting role falls back to model a, with
    # the drafting role's own time limit (the answer role has 1 s).
    loop, drafter = build(
        store,
        [READY],
        enabled=True,
        eligibleModelIds=["a", "b"],
        modelId="b",
        timeoutMs=5000,
        failure="fallback",
        fallbackModelId="a",
    )
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    [(_, timeout)] = drafter.calls
    assert timeout == 5.0
    assert change_requests.get(store, filed["id"])["payload"]["status"] == "ready"


def test_checked_issues_are_repaired_at_most_twice(store):
    issue = {"valid": False, "issues": [{"code": "graph", "message": "No path", "nodeId": "x"}]}
    gate = Gate([issue, issue, issue])
    loop, drafter = build(store, [READY, READY, READY, READY], gate)
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    payload = change_requests.get(store, filed["id"])["payload"]
    assert len(drafter.calls) == 3 and len(gate.checked) == 3
    assert shown(drafter, 1)["issues"] == ["Step x: No path"] and shown(drafter, 1)["previous"]
    assert payload["status"] == "ready" and payload["issues"] == ["Step x: No path"]


def test_a_repair_that_passes_the_check_stops_repairing(store):
    issue = {"valid": False, "issues": [{"code": "graph", "message": "No path", "nodeId": None}]}
    gate = Gate([issue])
    loop, drafter = build(store, [READY, READY], gate)
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    payload = change_requests.get(store, filed["id"])["payload"]
    assert len(drafter.calls) == 2 and payload["issues"] == [] and payload["checked"] is True


class Older:
    """A ToolGate from before the document check."""

    def tools(self):
        return list(TOOLS)


@pytest.mark.parametrize(
    "gate", [lambda: Gate([ToolGateUnavailable("check_404")]), Older], ids=["unreachable", "older"]
)
def test_without_toolgates_check_the_draft_is_ready_with_pis_own_issues(store, gate):
    gate = gate()
    unknown = json.loads(json.dumps(DOCUMENT))
    unknown["nodes"][2]["config"]["tool"] = "bank.transfer"
    loop, drafter = build(
        store, [json.dumps({"reply": "Reads.", "ready": True, "document": unknown})], gate
    )
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    payload = change_requests.get(store, filed["id"])["payload"]
    assert payload["status"] == "ready" and payload["checked"] is False
    assert payload["note"] == tool_drafts.NOT_CHECKED
    assert payload["issues"] == ["Step fetch uses 'bank.transfer', which is not an available tool."]
    assert len(drafter.calls) == 1


def test_a_question_from_the_drafter_fails_the_card_with_its_words(store):
    asked = json.dumps({"reply": "Which search engine?", "ready": False, "document": None})
    loop, _ = build(store, [asked])
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    assert change_requests.get(store, filed["id"])["payload"] == {
        "description": WANTED,
        "status": "failed",
        "error": "The drafter needs more detail: Which search engine?",
    }


def test_no_model_no_toolgate_and_no_tools_fail_in_plain_words(store, tmp_path):
    loop, _ = build(store, [], configured=False)
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    assert (
        "Choose one in Models settings"
        in change_requests.get(store, filed["id"])["payload"]["error"]
    )
    other = Store(tmp_path / "other.db")
    loop, _ = build(other, [], Gate(tools=None))
    filed = tool_drafts.file(other, WANTED)
    loop.drafts.tick()
    assert change_requests.get(other, filed["id"])["payload"]["error"] == tool_drafting.NO_TOOLGATE
    loop.toolgate = None
    again = tool_drafts.file(other, WANTED)
    loop.drafts.tick()
    assert change_requests.get(other, again["id"])["payload"]["error"] == tool_drafting.NO_TOOLS


def slow():
    """A model that keeps working until its call is stopped."""
    for _ in range(500):
        inference_control.check()
        time.sleep(0.01)
    return READY


def test_a_model_past_its_limit_fails_the_card(store):
    loop, _ = build(store, [slow])
    configure_answer_timeout(store, 100)
    filed = tool_drafts.file(store, WANTED)
    started = time.monotonic()
    loop.drafts.tick()
    assert time.monotonic() - started < 3
    payload = change_requests.get(store, filed["id"])["payload"]
    assert payload["status"] == "failed" and "time limit" in payload["error"]


def configure_answer_timeout(store, timeout):
    saved = model_roles.load(store)
    settings = saved["configuration"]
    settings["roleSettings"]["roles"]["answer"]["timeoutMs"] = timeout
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=saved["revision"],
            configuration=model_roles.Configuration.model_validate(settings),
        ),
    )


def test_a_chat_reply_on_the_local_model_pauses_the_draft_and_it_goes_on(store):
    def preempted():
        inference_control.current().cancel("preempted")
        inference_control.check()

    loop, drafter = build(store, [preempted, READY])
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.tick()
    assert len(drafter.calls) == 2
    assert change_requests.get(store, filed["id"])["payload"]["status"] == "ready"


def test_stopping_pi_cancels_the_draft_and_says_so(store):
    entered = threading.Event()

    def blocking():
        entered.set()
        return slow()

    loop, _ = build(store, [blocking])
    configure_answer_timeout(store, 120000)
    filed = tool_drafts.file(store, WANTED)
    loop.drafts.start()
    loop.drafts.kick()
    assert entered.wait(5)
    started = time.monotonic()
    loop.drafts.close()
    assert time.monotonic() - started < 3
    assert change_requests.get(store, filed["id"])["payload"] == tool_drafts.failed(
        WANTED, tool_drafts.RESTARTED
    )


def test_a_draft_left_running_when_pi_stopped_fails_on_start(store):
    filed = tool_drafts.file(store, WANTED)
    assert tool_drafts.recover_interrupted(store) == 1
    assert change_requests.get(store, filed["id"])["payload"] == {
        "description": WANTED,
        "status": "failed",
        "error": "Pi restarted while drafting. Try again.",
    }
    assert tool_drafts.pending(store) == []


def test_a_declined_request_is_not_overwritten_by_its_draft(store):
    loop, _ = build(store, [READY])
    filed = tool_drafts.file(store, WANTED)
    [claimed] = tool_drafts.pending(store)
    change_requests.decline(store, filed["id"])
    loop.drafts.draft(claimed)
    request = change_requests.get(store, filed["id"])
    assert request["state"] == "declined" and request["payload"]["status"] == "drafting"


def test_trying_again_drafts_from_the_new_description(store):
    loop, drafter = build(store, [json.dumps({"reply": "Which?", "ready": False}), READY])
    filed = tool_drafts.file(store, WANTED)
    with pytest.raises(tool_drafts.ToolDraftError) as busy:
        tool_drafts.retry(store, filed["id"], tool_drafts.Retry())
    assert busy.value.code == "drafting" and busy.value.status == 409
    loop.drafts.tick()
    again = tool_drafts.retry(store, filed["id"], tool_drafts.Retry(description="Search Bing"))
    assert again["payload"] == {"description": "Search Bing", "status": "drafting"}
    assert again["title"] == "Draft a tool: Search Bing"
    loop.drafts.tick()
    assert shown(drafter, 1)["description"] == "Search Bing"
    assert change_requests.get(store, filed["id"])["payload"]["status"] == "ready"
    same = tool_drafts.retry(store, filed["id"], tool_drafts.Retry())
    assert same["payload"] == {"description": "Search Bing", "status": "drafting"}
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            tool_drafts.Retry(description=blank)


def test_only_open_tool_requests_are_tried_again(store):
    memory = change_requests.file(store, "memory", "Remember: x", "x", {"text": "x"})
    with pytest.raises(tool_drafts.ToolDraftError) as wrong:
        tool_drafts.retry(store, memory["id"], tool_drafts.Retry())
    assert wrong.value.code == "not_a_tool_draft"
    filed = tool_drafts.file(store, WANTED)
    tool_drafts.recover_interrupted(store)
    change_requests.decline(store, filed["id"])
    with pytest.raises(tool_drafts.ToolDraftError) as decided:
        tool_drafts.retry(store, filed["id"], tool_drafts.Retry())
    assert decided.value.code == "decided"


def test_a_draft_is_saved_only_once_it_is_ready(store):
    filed = tool_drafts.file(store, WANTED)
    with pytest.raises(change_requests.ChangeRequestError) as early:
        change_requests.saved(store, filed["id"], change_requests.DraftSaved(draftId="x"))
    assert early.value.code == "not_ready"
    # A request filed before drafting moved to the background has no status: it is ready.
    older = change_requests.file(
        store, "tool_draft", "Add the tool", "", {"document": DOCUMENT, "issues": []}
    )
    assert tool_drafts.status(older) == "ready"
    done = change_requests.saved(store, older["id"], change_requests.DraftSaved(draftId="x"))
    assert done["result"] == {"draftId": "x"}


def test_a_new_draft_without_a_chat_is_idempotent_on_its_request_id(store):
    body = tool_drafts.NewDraft(description=" " + WANTED + " ", request_id="tool-draft-00001")
    first = tool_drafts.submit(store, body)
    assert first["payload"] == {"description": WANTED, "status": "drafting"}
    assert first["sessionId"] is None
    assert tool_drafts.submit(store, body)["id"] == first["id"]
    assert len(change_requests.listing(store)) == 1
    with pytest.raises(tool_drafts.ToolDraftError) as reused:
        tool_drafts.submit(
            store, tool_drafts.NewDraft(description="Other", request_id="tool-draft-00001")
        )
    assert reused.value.code == "request_reused" and reused.value.status == 409
    for values in (
        {"description": "", "request_id": "x" * 16},
        {"description": "x", "request_id": "short"},
    ):
        with pytest.raises(ValueError):
            tool_drafts.NewDraft(**values)


def test_publishing_tells_the_chat_that_asked_once(store):
    session = store.create_session()
    filed = tool_drafts.file(store, WANTED, session_id=session)
    body = tool_drafts.Published(toolId=f"workflow:topic-reader:1:{DIGEST}", name="Topic reader")
    request, first = tool_drafts.published(store, filed["id"], body, now=5.0)
    assert first and request["state"] == "approved"
    assert request["result"] == {
        "published": {"toolId": body.toolId, "name": "Topic reader", "publishedAt": 5.0}
    }
    tool_drafting.announce(store, session, "Topic \x1b[2Jreader")
    [note] = store.messages(session)
    assert note["role"] == "assistant"
    assert note["content"] == "“Topic [2Jreader” is live. Want me to run it now?"
    again, first = tool_drafts.published(store, filed["id"], body)
    assert not first and again["result"] == request["result"]


def test_a_saved_request_records_its_publication_beside_the_draft(store):
    older = change_requests.file(
        store, "tool_draft", "Add the tool", "", {"document": DOCUMENT, "issues": []}
    )
    change_requests.saved(store, older["id"], change_requests.DraftSaved(draftId="topic"))
    body = tool_drafts.Published(toolId="workflow-x", name="Topic reader")
    request, first = tool_drafts.published(store, older["id"], body, now=1.0)
    assert first and request["result"]["draftId"] == "topic"
    memory = change_requests.file(store, "memory", "Remember: x", "x", {"text": "x"})
    with pytest.raises(tool_drafts.ToolDraftError) as wrong:
        tool_drafts.published(store, memory["id"], body)
    assert wrong.value.code == "not_a_tool_draft"


def test_the_note_waits_for_a_busy_chat_and_follows_a_fork(store, monkeypatch):
    parent = store.create_session()
    store.close_session(parent, "forked")
    child = store.create_session(parent_id=parent)
    turn = store.start_turn(child)
    monkeypatch.setattr(tool_drafting.answers, "background", lambda work: work())
    waited = []

    def finish(_seconds):
        waited.append(_seconds)
        store.finish_turn(turn, "complete")

    assert tool_drafting.announce(store, parent, "Topic reader", sleep=finish) is None
    assert waited == [tool_drafting.BUSY_WAIT]
    [note] = [m for m in store.messages(child) if m["role"] == "assistant"]
    assert note["content"].startswith("“Topic reader” is live.")
    assert store.messages(parent) == []


def test_relevance_counts_rare_words_more_and_keeps_at_most_eight():
    tools = [
        Tool(f"search.{n}", f"Search {n}", "Search something", [{"name": "query"}], "auto")
        for n in range(12)
    ]
    tools.append(Tool("web.lookup", "Web lookup", "Search the web", [{"name": "query"}], "auto"))
    capabilities = draft_capabilities.available(tools)
    picked = draft_capabilities.relevant(capabilities, "search the web")
    assert len(picked) == 8 and picked[0]["id"] == "web.lookup"
    assert draft_capabilities.relevant(capabilities, "zzz") == capabilities
    assert draft_capabilities.words("Disks and the disk") == {"disk"}


def test_steps_read_every_kind_of_step_in_running_order():
    document = {
        "inputs": [{"name": "a"}, {"name": "b"}],
        "nodes": [
            {"id": "end", "type": "return", "label": "End", "config": {}},
            {"id": "start", "type": "input", "label": "Start", "config": {}},
            {
                "id": "sum",
                "type": "calculation",
                "label": "Sum",
                "config": {"operator": "add", "left": "$input.a", "right": 2},
            },
            {
                "id": "big",
                "type": "condition",
                "label": "Big?",
                "config": {"operator": "greater", "left": "$steps.sum.value", "right": 10},
            },
            {
                "id": "each",
                "type": "loop",
                "label": "Each",
                "config": {"items": "$input.b", "limit": 5, "operation": "trim"},
            },
            {"id": "note", "type": "set", "label": "Note", "config": {"value": "Cost $$5"}},
            {
                "id": "brief",
                "type": "workflow_call",
                "label": "Brief",
                "config": {"toolId": "daily-brief", "version": 3, "args": {}},
            },
        ],
        "edges": [
            {"source": "start", "target": "sum"},
            {"source": "sum", "target": "big"},
            {"source": "big", "target": "each", "branch": "true"},
            {"source": "big", "target": "note", "branch": "false"},
            {"source": "each", "target": "brief"},
            {"source": "note", "target": "end"},
            {"source": "brief", "target": "end"},
        ],
    }
    capabilities = [{"id": "daily-brief", "name": "Daily brief", "authorization": "ask"}]
    assert draft_capabilities.steps(document, capabilities) == [
        "Start with the a and the b",
        "Work out the a plus 2",
        "Check whether the value from “Sum” is more than 10",
        "Go through each item in the b, at most 5",
        "Note: set “Cost $5”",
        "Brief (uses Daily brief, asks you first)",
        "Finish",
    ]
