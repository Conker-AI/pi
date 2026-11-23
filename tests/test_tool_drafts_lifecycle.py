"""Tool drafts over time: limits, pausing, stopping and restarting, retries, publication and the notes."""

import json
import threading
import time

import pytest

from pi.actions.toolgate import Tool
from pi.agents import change_requests, draft_capabilities, tool_drafts
from pi.models import inference_control
from pi.models import roles as model_roles
from pi.turns import tool_drafting
from tests.tool_drafts_helpers import (
    DIGEST,
    DOCUMENT,
    READY,
    WANTED,
    build,
    shown,
    store,  # noqa: F401 -- shared fixture
)


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
