"""The owner import routes describe their replies in OpenAPI (D-01), and what they send matches
the description: two imports, offers sent, refused and waiting, pages of chats and messages, a
deletion and its repeat, memory skipped at setup and no import listener at all."""

import pytest
from test_assistant_import_api import http as http
from test_assistant_import_api import listener as listener
from test_assistant_import_api import offers as offers
from test_assistant_import_api import owner
from test_assistant_import_api import store as store
from test_assistant_imports import chatgpt_chat, claude_chat, raw

from pi.api import server as api
from pi.memory import import_replies as replies

ROUTES = {
    ("post", "/imports/{source}/{digest}"): replies.ImportBatchResult,
    ("get", "/imports"): replies.ImportHistory,
    ("get", "/imports/chats"): replies.ImportedChatList,
    ("get", "/imports/chats/{chat_id}"): replies.ImportedChatTranscript,
    ("post", "/imports/chats/{chat_id}/delete"): replies.ImportedChatDeletion,
}
LONG = "x" * 17000  # kept whole, but too long to offer to memory


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_each_route_names_its_reply_model_in_openapi(method, path):
    operation = api.app.openapi()["paths"][path][method]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{ROUTES[(method, path)].__name__}"}


def ok(reply, model):
    assert reply.status_code == 200, reply.text
    return model.model_validate_json(reply.text)


def imported(http, source, chats):
    body = raw(chats)
    digest = http.post(f"/imports/{source}/preview", content=body, headers=owner()).json()["digest"]
    reply = http.post(f"/imports/{source}/{digest}", content=body, headers=owner())
    return ok(reply, replies.ImportBatchResult)


def test_every_import_reply_matches_its_model(http, store, offers, listener, monkeypatch):
    turns = [("human", "Help me"), ("assistant", "Sure."), ("human", LONG), ("human", "Again")]
    first = imported(http, "claude", [claude_chat("k1", turns=turns), claude_chat("k2")])
    assert first.memory.on and first.chats.new == 2
    listener.answers = [(422, {"detail": "bad"})]
    assert offers.drain_once(limit=2) == 1  # one refused, one sent, the rest still waiting
    with store._connect() as db:
        db.execute("INSERT INTO setup_optional_choices VALUES('memory',1,'choice_skip','skip',0)")
    second = imported(http, "chatgpt", [chatgpt_chat("c1")])
    assert not second.memory.on and second.memory.reason
    history = ok(http.get("/imports", headers=owner()), replies.ImportHistory)
    assert [line.batchId for line in history.imports] == [second.batchId, first.batchId]

    page = ok(http.get("/imports/chats?limit=1", headers=owner()), replies.ImportedChatList)
    assert page.nextOffset == 1
    every = ok(http.get("/imports/chats", headers=owner()), replies.ImportedChatList)
    assert every.nextOffset is None and every.total == 3
    states, sent = set(), []
    for summary in every.chats:
        path = f"/imports/chats/{summary.id}"
        part = ok(http.get(path + "?limit=1", headers=owner()), replies.ImportedChatTranscript)
        assert part.nextAfter == 1
        whole = ok(http.get(path, headers=owner()), replies.ImportedChatTranscript)
        assert whole.nextAfter is None
        states |= {message.memory for message in whole.messages}
        sent += [summary.id] if any(m.memory == "offered" for m in whole.messages) else []
    assert states == {"offering", "offered", "refused", "not_offered", None}

    def delete(chat_id):
        reply = http.post(f"/imports/chats/{chat_id}/delete", headers=owner())
        return ok(reply, replies.ImportedChatDeletion)

    (offered,) = sent
    gone = delete(offered)
    assert gone.memoryNote and isinstance(gone.fileScrubbed, bool)
    assert delete(offered).fileScrubbed is None
    c1 = next(chat.id for chat in every.chats if chat.exportId == "c1")
    assert delete(c1).memoryNote is None

    erasing = ok(http.get("/imports", headers=owner()), replies.ImportHistory)
    assert erasing.memory.erasing == 1
    monkeypatch.setattr(api.app.state, "import_offers", None, raising=False)
    unset = ok(http.get("/imports", headers=owner()), replies.ImportHistory)
    assert not unset.memory.configured and unset.memory.problem
