"""Importing ChatGPT and Claude exports: strict reading, preview, idempotent import (gap 9).

Every export here is synthetic and tiny. The readers keep only what either side said in the chat
as it was shown, refuse a file that is not an export, and leave out (and count) a chat that does
not have the expected shape.
"""

import json
from contextlib import closing

import pytest

from pi.conversation.store import Store
from pi.memory import import_formats, imports
from pi.memory.import_formats import FormatRefused
from pi.owner import settings as owner_settings

START = 1_700_000_000


def chatgpt_chat(cid="c1", title="Trip plans", turns=None, start=START):
    """A ChatGPT export chat: a mapping of nodes, the shown chat ending at current_node."""
    turns = turns or [("user", "Plan a trip to Riga for me"), ("assistant", "Here is a plan.")]
    mapping = {"root": {"id": "root", "message": None, "parent": None, "children": []}}
    parent = "root"
    for index, (role, text) in enumerate(turns):
        node = f"{cid}-n{index}"
        content = text if isinstance(text, dict) else {"content_type": "text", "parts": [text]}
        mapping[node] = {
            "id": node,
            "parent": parent,
            "children": [],
            "message": {
                "id": f"{cid}-m{index}",
                "author": {"role": role, "name": None, "metadata": {}},
                "create_time": start + index,
                "content": content,
                "status": "finished_successfully",
                "metadata": {},
                "recipient": "all",
            },
        }
        mapping[parent]["children"].append(node)
        parent = node
    return {
        "title": title,
        "create_time": start,
        "update_time": start + len(turns),
        "mapping": mapping,
        "current_node": parent,
        "conversation_id": cid,
        "id": cid,
        "default_model_slug": "gpt-4o",
    }


def claude_chat(uid="k1", name="Essay help", turns=None):
    turns = turns or [("human", "Help me with my essay"), ("assistant", "Sure, send it.")]
    return {
        "uuid": uid,
        "name": name,
        "created_at": "2024-05-01T10:00:00.000000Z",
        "updated_at": "2024-05-01T10:30:00.000000Z",
        "account": {"uuid": "acct-1"},
        "chat_messages": [
            {
                "uuid": f"{uid}-m{index}",
                "sender": sender,
                "text": text,
                "content": [{"type": "text", "text": text}],
                "created_at": f"2024-05-01T10:0{index}:00.123456Z",
                "attachments": [],
                "files": [],
            }
            for index, (sender, text) in enumerate(turns)
        ],
    }


def raw(chats) -> bytes:
    return json.dumps(chats).encode()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_chatgpt_keeps_the_shown_branch_and_what_either_side_said():
    chat = chatgpt_chat(
        turns=[
            ("system", "You are ChatGPT"),
            ("user", "What is in this photo?"),
            ("assistant", "A cat."),
        ]
    )
    # An edited message leaves an older branch behind; it is not the chat as shown.
    chat["mapping"]["old"] = {
        "id": "old",
        "parent": "c1-n0",
        "children": [],
        "message": {
            "id": "old-m",
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["an older draft"]},
        },
    }
    chat["mapping"]["c1-n1"]["message"]["content"] = {
        "content_type": "multimodal_text",
        "parts": [{"content_type": "image_asset_pointer", "asset_pointer": "file://x"}, "Look"],
    }
    tool = {"content_type": "code", "text": "print(1)"}
    chat["mapping"]["c1-n2"]["children"] = ["tool"]
    chat["mapping"]["tool"] = {
        "id": "tool",
        "parent": "c1-n2",
        "children": [],
        "message": {"id": "t", "author": {"role": "tool"}, "content": tool},
    }
    chat["current_node"] = "tool"
    export = import_formats.read("chatgpt", raw([chat]))
    (only,) = export.chats
    assert [(m.author, m.text) for m in only.messages] == [
        ("user", "[image]\nLook"),
        ("assistant", "A cat."),
    ]
    assert only.export_id == "c1" and only.title == "Trip plans" and only.started_at == START
    assert export.skipped["toolOrHiddenMessages"] == 2


def test_chatgpt_hidden_notes_and_tool_calls_are_left_out():
    chat = chatgpt_chat(turns=[("user", "hi"), ("assistant", "hello")])
    chat["mapping"]["c1-n0"]["message"]["metadata"] = {"is_visually_hidden_from_conversation": True}
    chat["mapping"]["c1-n1"]["message"]["recipient"] = "browser"
    export = import_formats.read("chatgpt", raw([chat, chatgpt_chat("c2")]))
    assert [chat.export_id for chat in export.chats] == ["c2"]
    assert export.skipped["emptyChats"] == 1 and export.skipped["toolOrHiddenMessages"] == 2


def test_claude_keeps_text_blocks_and_only_the_names_of_files():
    chat = claude_chat(turns=[("human", "Read my notes"), ("assistant", "Done")])
    chat["chat_messages"][0]["attachments"] = [
        {"file_name": "notes.txt", "extracted_content": "SECRET FILE BODY"}
    ]
    chat["chat_messages"][1]["content"] = [
        {"type": "thinking", "thinking": "private reasoning"},
        {"type": "text", "text": "Done"},
        {"type": "tool_use", "name": "x", "input": {}},
        {"type": "text", "text": "Anything else?"},
    ]
    tool_only = {
        "uuid": "k1-m9",
        "sender": "assistant",
        "text": "",
        "content": [{"type": "tool_result", "content": []}],
        "created_at": "2024-05-01T10:09:00Z",
    }
    chat["chat_messages"].append(tool_only)
    export = import_formats.read("claude", raw([chat]))
    (only,) = export.chats
    assert [(m.author, m.text) for m in only.messages] == [
        ("user", "Read my notes\n\n[file: notes.txt]"),
        ("assistant", "Done\n\nAnything else?"),
    ]
    assert "SECRET" not in repr(export) and "private reasoning" not in repr(export)
    assert only.started_at == pytest.approx(1714557600.0)
    assert export.skipped["toolOrHiddenMessages"] == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"", "not_a_list"),
        (b'{"conversations": []}', "not_a_list"),
        (b"[{]", "not_json"),
        (b"[1,]", "not_json"),
        (b"[] []", "not_json"),
        (b'[{"create_time": NaN}]', "not_json"),
        (b"\xff\xfe[]", "not_json"),
        (b"[" * 100000 + b"]" * 100000, "not_json"),
    ],
    ids=["empty", "object", "broken", "trailing comma", "two lists", "nan", "utf16", "deep"],
)
def test_a_file_that_is_not_an_export_is_refused_whole(body, code):
    with pytest.raises(FormatRefused) as refused:
        import_formats.read("chatgpt", body)
    assert refused.value.code == code and refused.value.status == 422


def test_a_bom_and_an_empty_list_are_fine():
    export = import_formats.read("claude", b"\xef\xbb\xbf[ ]")
    assert export.chats == () and export.skipped["chatsNotMatching"] == 0


def test_the_wrong_kind_of_export_says_which_it_looks_like():
    with pytest.raises(FormatRefused) as refused:
        import_formats.read("chatgpt", raw([claude_chat()]))
    assert refused.value.code == "wrong_format"
    assert "looks like a Claude export" in refused.value.message
    with pytest.raises(FormatRefused) as refused:
        import_formats.read("claude", raw([chatgpt_chat()]))
    assert "looks like a ChatGPT export" in refused.value.message


@pytest.mark.parametrize(
    "spoil",
    [
        lambda chat: chat.update(current_node="missing"),
        lambda chat: chat.update(conversation_id="../../etc", id=None),
        lambda chat: chat.update(create_time="yesterday"),
        lambda chat: chat.update(create_time=True),
        lambda chat: chat.update(update_time=1e300),
        lambda chat: chat["mapping"]["root"].update(parent="c1-n1"),  # a loop
        lambda chat: chat["mapping"]["c1-n1"]["message"].update(author="user"),
        lambda chat: chat["mapping"]["c1-n1"]["message"]["content"].update(parts=[1]),
        lambda chat: chat["mapping"].update({"c1-n1": "not a node"}),
    ],
)
def test_a_chat_with_the_wrong_shape_is_left_out_and_counted(spoil):
    bad = chatgpt_chat("bad")
    bad["mapping"] = json.loads(json.dumps(chatgpt_chat("c1")["mapping"]))
    bad["current_node"] = "c1-n1"
    spoil(bad)
    export = import_formats.read("chatgpt", raw([bad, chatgpt_chat("good")]))
    assert [chat.export_id for chat in export.chats] == ["good"]
    assert export.skipped["chatsNotMatching"] == 1


@pytest.mark.parametrize(
    "spoil",
    [
        lambda chat: chat.update(uuid=7),
        lambda chat: chat.update(created_at="not a date"),
        lambda chat: chat.update(chat_messages={"a": 1}),
        lambda chat: chat["chat_messages"][0].update(sender="system"),
        lambda chat: chat["chat_messages"][0].update(content=[{"type": "text", "text": 5}]),
        lambda chat: chat["chat_messages"][0].update(attachments=[{"file_name": 3}]),
    ],
)
def test_a_claude_chat_with_the_wrong_shape_is_left_out(spoil):
    bad = claude_chat("bad")
    spoil(bad)
    export = import_formats.read("claude", raw([bad, claude_chat("good")]))
    assert [chat.export_id for chat in export.chats] == ["good"]


def test_duplicates_in_one_file_are_kept_once():
    chat = claude_chat(turns=[("human", "same"), ("assistant", "reply")])
    chat["chat_messages"].append(dict(chat["chat_messages"][0]))
    export = import_formats.read("claude", raw([chat, claude_chat()]))
    assert len(export.chats) == 1 and len(export.chats[0].messages) == 2
    assert export.skipped["duplicateChats"] == 1 and export.skipped["duplicateMessages"] == 1


def test_limits_refuse_oversized_files(monkeypatch):
    body = raw([chatgpt_chat("a"), chatgpt_chat("b"), chatgpt_chat("c")])
    monkeypatch.setattr(import_formats, "MAX_BYTES", len(body) - 1)
    with pytest.raises(FormatRefused) as refused:
        import_formats.read("chatgpt", body)
    assert refused.value.code == "too_large"
    monkeypatch.setattr(import_formats, "MAX_BYTES", len(body))
    monkeypatch.setattr(import_formats, "MAX_CHATS", 2)
    with pytest.raises(FormatRefused) as refused:
        import_formats.read("chatgpt", body)
    assert refused.value.code == "too_many_chats"
    monkeypatch.setattr(import_formats, "MAX_CHATS", 3)
    monkeypatch.setattr(import_formats, "MAX_MESSAGES", 5)
    with pytest.raises(FormatRefused) as refused:
        import_formats.read("chatgpt", body)
    assert refused.value.code == "too_many_messages"


def test_long_chats_are_left_out_and_long_messages_shortened(monkeypatch):
    monkeypatch.setattr(import_formats, "MAX_CHAT_MESSAGES", 2)
    monkeypatch.setattr(import_formats, "MAX_TEXT", 10)
    long = claude_chat("long", turns=[("human", "a"), ("assistant", "b"), ("human", "c")])
    wordy = claude_chat("wordy", turns=[("human", "x" * 25)])
    export = import_formats.read("claude", raw([long, wordy]))
    (chat,) = export.chats
    assert chat.messages[0].shortened and chat.messages[0].text.startswith("x" * 10 + "\n\n")
    assert "15 more characters" in chat.messages[0].text
    assert export.skipped["longChats"] == 1 and export.skipped["shortenedMessages"] == 1
    assert not imports.offerable(chat.messages[0])


def test_text_is_cleaned_of_nul_and_unpaired_surrogates():
    body = b'[{"uuid":"k1","name":"t\\u0000","chat_messages":[{"uuid":"m1","sender":"human",'
    body += b'"text":"bad \\ud800 half\\u0000 ok","created_at":null}]}]'
    (chat,) = import_formats.read("claude", body).chats
    assert chat.title == "t" and chat.messages[0].text == "bad � half ok"
    chat.messages[0].text.encode("utf-8")


def test_preview_saves_nothing_and_says_what_an_import_would_add(store):
    body = raw([chatgpt_chat("c1", "Old trip"), chatgpt_chat("c2", "Newer trip", start=START + 99)])
    before = _counts(store)
    view = imports.preview(store, "chatgpt", body, memory_configured=False)
    assert _counts(store) == before
    assert view["label"] == "Imported from ChatGPT" and view["digest"] == imports.digest(body)
    assert view["chats"] == {"inFile": 2, "new": 2, "updated": 0, "unchanged": 0}
    assert view["messages"] == {"inFile": 4, "new": 4, "yours": 2, "offeredToMemory": 2}
    assert view["sampleTitles"] == ["Newer trip", "Old trip"]
    assert view["firstAt"] == START and view["lastAt"] == START + 101
    assert view["memory"] == {"on": True, "reason": None, "configured": False}


def test_import_is_bound_to_the_previewed_file_and_idempotent(store):
    body = raw([claude_chat("k1"), claude_chat("k2", "Second")])
    with pytest.raises(imports.ImportRefused) as refused:
        imports.save(store, "claude", body, "0" * 64, memory_configured=True)
    assert refused.value.code == "file_changed" and refused.value.status == 409
    assert _counts(store)["imported_chats"] == 0
    first = imports.save(store, "claude", body, imports.digest(body), memory_configured=True)
    assert first["chats"] == {"new": 2, "updated": 0, "unchanged": 0}
    assert first["messages"] == {"added": 4, "offeredToMemory": 2}
    again = imports.save(store, "claude", body, imports.digest(body), memory_configured=True)
    assert again["chats"] == {"new": 0, "updated": 0, "unchanged": 2}
    assert again["messages"] == {"added": 0, "offeredToMemory": 0}
    assert _counts(store) == {
        "imported_chats": 2,
        "imported_messages": 4,
        "import_memory_offers": 2,
        "sessions": 0,
        "messages": 0,
        "memory_outbox": 0,
    }
    assert imports.preview(store, "claude", body, memory_configured=True)["chats"] == {
        "inFile": 2,
        "new": 0,
        "updated": 0,
        "unchanged": 2,
    }
    # A later export of a chat that went on adds only what is new, after what was there.
    longer = claude_chat(
        "k1",
        turns=[
            ("human", "Help me with my essay"),
            ("assistant", "Sure, send it."),
            ("human", "Here"),
        ],
    )
    later = raw([longer])
    result = imports.save(store, "claude", later, imports.digest(later), memory_configured=True)
    assert result["chats"]["updated"] == 1 and result["messages"]["added"] == 1
    listing = imports.chats(store, source="claude")
    assert listing["total"] == 2
    chat = imports.chat(store, next(c["id"] for c in listing["chats"] if c["exportId"] == "k1"))
    assert [m["text"] for m in chat["messages"]][-1] == "Here"
    assert [m["seq"] for m in chat["messages"]] == [1, 2, 3]
    assert [b["chats"]["new"] for b in imports.batches(store)] == [0, 0, 2]


def test_imported_chats_are_labelled_read_only_and_keep_their_source(store):
    body = raw([chatgpt_chat("c1")])
    imports.save(store, "chatgpt", body, imports.digest(body), memory_configured=False)
    (listed,) = imports.chats(store)["chats"]
    assert listed["label"] == "Imported from ChatGPT" and listed["exportId"] == "c1"
    assert listed["archived"] is True and listed["readOnly"] is True
    chat = imports.chat(store, listed["id"], limit=1)
    assert chat["messages"][0]["memory"] == "offering" and chat["nextAfter"] == 1
    rest = imports.chat(store, listed["id"], after=1)
    assert rest["messages"][0]["author"] == "assistant" and rest["messages"][0]["memory"] is None
    assert rest["nextAfter"] is None
    with store._connect() as db, pytest.raises(Exception, match="stay as they were"):
        db.execute("UPDATE imported_messages SET text='rewritten'")
    with pytest.raises(imports.ImportRefused) as missing:
        imports.chat(store, "imp_" + "0" * 32)
    assert missing.value.status == 404


@pytest.mark.parametrize("setting", ["setup_skipped", "private_by_default"])
def test_nothing_is_offered_to_memory_when_he_keeps_new_chats_out_of_it(store, setting):
    if setting == "setup_skipped":
        with store._connect() as db:
            db.execute(
                "INSERT INTO setup_optional_choices VALUES ('memory',1,'choice-request-0001','skip',0)"
            )
    else:
        owner_settings.save(
            store,
            owner_settings.Update(
                expected_revision=owner_settings.load(store)["revision"],
                values={"chats.default_incognito": True},
            ),
        )
    body = raw([chatgpt_chat("c1")])
    view = imports.preview(store, "chatgpt", body, memory_configured=True)
    assert view["memory"]["on"] is False and view["messages"]["offeredToMemory"] == 0
    result = imports.save(store, "chatgpt", body, imports.digest(body), memory_configured=True)
    assert result["messages"] == {"added": 2, "offeredToMemory": 0}
    assert _counts(store)["import_memory_offers"] == 0


def _counts(store):
    with store._connect() as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "imported_chats",
                "imported_messages",
                "import_memory_offers",
                "sessions",
                "messages",
                "memory_outbox",
            )
        }
