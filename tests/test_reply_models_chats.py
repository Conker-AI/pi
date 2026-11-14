"""The chat routes describe their replies in OpenAPI (D-01), and what they send matches the
description: the chat, settings, context, summary, archive, trash and new-chat draft flows of
other test modules run again, and each 200 reply of one of these routes is validated against
the model its route names (the route is found by Pi's own routing, so the template is exact)."""

import contextlib
import importlib
import inspect
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match
from test_loop import Recorder, loop_with
from test_response_versions import alternative

from pi.api import server as api
from pi.context import context_replies as context
from pi.conversation import chat_replies as chats
from pi.conversation import session_replies as sessions
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.turns import action_identities as identities

MODELS = {
    ("GET", "/sessions/{session_id}"): sessions.ChatSessionDocument,
    ("GET", "/sessions/{identity}/settings"): chats.ChatSettingsDocument,
    ("POST", "/sessions/{identity}/settings"): chats.ChatSettingsDocument,
    ("POST", "/sessions/archive"): chats.ChatArchiveResult,
    ("POST", "/sessions/trash"): chats.ChatTrashResult,
    ("GET", "/context/{session_id}"): context.ChatContextPolicyDocument,
    ("POST", "/context/{session_id}"): context.ChatContextPolicyDocument,
    ("GET", "/context/{session_id}/summary"): context.ChatSummaryDocument,
    ("POST", "/context/{session_id}/summary"): context.ChatSummaryDocument,
    ("GET", "/drafts/new"): chats.NewChatDraftDocument,
    ("POST", "/drafts/new"): chats.NewChatDraftDocument,
    ("POST", "/drafts/new/start"): chats.NewChatStartDocument,
    ("GET", "/drafts/new/start/{request_id}"): chats.NewChatStartDocument,
}
# Flows of other modules that reach these routes: archive, trash, new-chat drafts, chat settings,
# context reads, memory delivery and a forgotten chat; a name after "::" runs only that flow.
# The chat in many other states (versions, citations, files, replies, approvals, thinking, a
# pending input, saved context and summary) is built by `many_states` below.
FLOW_MODULES = (
    "test_chat_archive::test_api_route_is_owner_only_and_history_takes_the_new_parameters",
    "test_chat_trash::test_trash_is_an_owner_write_without_a_password",
    "test_private_drafts",
    "test_session_settings",
    "test_owner_context_read",
    "test_memory_status_once::test_opening_a_chat_reads_delivery_once",
    "test_forgetting::test_citation_http_distinguishes_forgotten_from_missing_and_cannot_delete",
)
ADMIN = "reply-models-chats-admin"
FIXTURES = {"tmp_path", "monkeypatch"}


def template(method, path):
    scope = {"type": "http", "path": path, "method": method}
    for route in api.app.routes:
        match = route.matches(scope)[0]
        # FastAPI wraps include_router() in _IncludedRouter, whose matches() hides the route.
        while match == Match.FULL and type(route).__name__ == "_IncludedRouter":
            match, _, route, _ = route._match(scope)
        if match == Match.FULL:
            return route.path
    return None


@pytest.mark.parametrize(("method", "path"), MODELS)
def test_each_route_names_its_reply_model_in_openapi(method, path):
    operation = api.app.openapi()["paths"][path][method.lower()]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{MODELS[method, path].__name__}"}


def fixture(module, name):
    """The plain function behind a module's own fixture, or None (conftest and parametrized
    fixtures are not resolved here; a flow that needs one is left out)."""
    value = getattr(module, name, None)
    if type(value).__name__ != "FixtureFunctionDefinition":
        return None
    if value._fixture_function_marker.params is not None:
        return None
    return value._get_wrapped_function()


def resolve(module, name, arguments, stack, cache):
    """Call `name` with tmp_path, monkeypatch and the module's own fixtures, as pytest would."""
    if name in arguments:
        return arguments[name]
    if name in cache:
        return cache[name]
    function = fixture(module, name)
    if function is None:
        raise LookupError(name)
    values = {
        key: resolve(module, key, arguments, stack, cache)
        for key in inspect.signature(function).parameters
    }
    value = function(**values)
    if inspect.isgenerator(value):
        generator = value
        value = next(generator)
        stack.callback(lambda: next(generator, None))
    cache[name] = value
    return value


def runnable(module, test):
    pending, seen = list(inspect.signature(test).parameters), set()
    while pending:
        name = pending.pop()
        if name in FIXTURES or name in seen:
            continue
        seen.add(name)
        function = fixture(module, name)
        if function is None:
            return False
        pending.extend(inspect.signature(function).parameters)
    return True


def flows():
    for entry in FLOW_MODULES:
        name, _, only = entry.partition("::")
        module = importlib.import_module(name)
        for test_name, test in vars(module).items():
            if not test_name.startswith("test_") or not inspect.isfunction(test):
                continue
            if only and test_name != only:
                continue
            if getattr(test, "pytestmark", None) or not runnable(module, test):
                continue
            yield module, test


def many_states(tmp_path, monkeypatch):
    """One chat with an answer in two versions, a cited answer, a reply that quotes an earlier
    message and carries a file, a parked action, recalled memory, background thinking and an
    input that failed to prepare; its context policy and summary are saved through their routes."""
    store = Store(tmp_path / "pi.db")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "loop", loop_with(store, Recorder("Answer")), raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
    http = TestClient(api.app, headers={"X-Pi-Key": ADMIN})
    sid = store.create_session(title="Many states")
    budget = {"contextWindowTokens": 100000, "outputReserveTokens": 1000, "otherInputTokens": 0}
    policy = {"sessionInstructions": "Be brief.", "messagePolicies": {}, "budget": budget}
    assert http.post(f"/context/{sid}", json={"expected_revision": 0, "policy": policy}).is_success
    summary = {"expected_revision": 0, "summary": "Earlier talk."}
    assert http.post(f"/context/{sid}/summary", json=summary).is_success
    first = http.post(f"/sessions/{sid}/turns", json={"text": "First question"}).json()
    alternative(store, sid, first["message"]["id"])
    asked = next(m for m in store.messages(sid) if m["role"] == "user")
    upload = http.post(
        f"/sessions/{sid}/attachments",
        params={"name": "notes.txt", "type": "text/plain"},
        content=b"Plain notes.",
    ).json()
    reply = {"text": "About these notes", "attachment_ids": [upload["id"]], "reply_to": asked["id"]}
    assert http.post(f"/sessions/{sid}/turns", json=reply).is_success
    cited = store.start_turn(sid)
    store.complete_turn(cited, "Cited answer", citations=[{"id": "c1", "label": "Source"}])
    memory_store.save_context(store, cited, "ok", {"memories": [{"text": "A fact."}]})
    parked = store.start_turn(sid)
    identities.prepare(store, parked, "demo.tool", {"x": 1}, "act_" + "0" * 32)
    store.finish_turn(
        parked,
        "awaiting_approval",
        approval_request_id="req_1",
        approval_tool_id="demo.tool",
        approval_args=json.dumps({"x": 1}),
        approval_expires_at="2026-11-14T00:00:00Z",
        approval_intent="Do x",
    )
    now = time.time()
    with store._connect() as db:
        # The columns pi.turns.thinking writes: a queued row, then one it finished.
        db.execute(
            "INSERT INTO background_thinking(turn_id,session_id,status,created_at,detail,model,"
            "provider) VALUES(?,?,'queued',?,'Waiting.','m','p')",
            (parked, sid, now),
        )
        db.execute(
            "INSERT INTO background_thinking VALUES(?,?,'complete',?,?,'m','p','Found.',NULL,3,4)",
            (cited, sid, now, now + 1),
        )
        db.execute(
            "INSERT INTO turn_submissions(request_id,requested_session_id,state,history_seq,"
            "created_at,updated_at,failure_code) VALUES(?,?,'preparation_failed',0,?,?,'x')",
            ("req_" + "1" * 16, sid, now, now),
        )
    return http, sid


def test_every_chat_reply_matches_its_model(tmp_path, monkeypatch):
    seen: dict[tuple[str, str], int] = {}
    keys: set[str] = set()
    lists: dict[str, bool] = {}
    original = TestClient.request

    def checked(self, method, url, *args, **kwargs):
        reply = original(self, method, url, *args, **kwargs)
        if self.app is api.app and reply.status_code == 200:
            key = (method.upper(), template(method.upper(), reply.request.url.path))
            if key in MODELS:
                MODELS[key].model_validate_json(reply.text)
                seen[key] = seen.get(key, 0) + 1
                if key[1] == "/sessions/{session_id}":
                    chat = json.loads(reply.text)
                    for message in chat["messages"]:
                        keys.update(message)
                    for name in ("pending_submissions", "thinking", "turns"):
                        lists[name] = lists.get(name, False) or bool(chat[name])
                    lists["action"] = lists.get("action", False) or any(
                        turn["action"] for turn in chat["turns"]
                    )
        return reply

    monkeypatch.setattr(TestClient, "request", checked)
    ran = {entry.partition("::")[0]: 0 for entry in FLOW_MODULES}
    for index, (module, test) in enumerate(flows()):
        folder = tmp_path / str(index)
        folder.mkdir()
        with pytest.MonkeyPatch.context() as patch, contextlib.ExitStack() as stack:
            arguments, cache = {"tmp_path": folder, "monkeypatch": patch}, {}
            try:
                values = {
                    key: resolve(module, key, arguments, stack, cache)
                    for key in inspect.signature(test).parameters
                }
                test(**values)
            except pytest.skip.Exception:
                continue
        ran[module.__name__] += 1
    (tmp_path / "many").mkdir()
    http, sid = many_states(tmp_path / "many", monkeypatch)
    for path in (f"/sessions/{sid}", f"/context/{sid}", f"/context/{sid}/summary", "/drafts/new"):
        assert http.get(path).status_code == 200
    # Every listed module (or named flow) still exists and ran.
    assert all(ran.values()) and sum(ran.values()) >= 30, ran
    assert set(MODELS) <= set(seen), sorted(set(MODELS) - set(seen))
    # Every optional message key and every list was filled in some reply, so each was checked.
    assert {
        "content_status",
        "receipt_id",
        "forgotten_at",
        "citations",
        "memory_delivery",
        "response_family",
        "turn_id",
        "agent_id",
        "reply_to",
        "attachments",
    } <= keys, keys
    assert all(lists.values()) and len(lists) == 4, lists
