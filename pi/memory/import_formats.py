"""Strict readers for ChatGPT and Claude data exports: bounded, inert, never trusted.

An export is a file the owner downloaded from another assistant. Pi reads only chat ids, titles,
times and the text of the messages a person could see; it follows no link, loads no attachment
and runs nothing in the file. Whatever the file says, it stays outside content: nothing here can
make its words the owner's own message to Conker.

ChatGPT: `conversations.json` from the export zip, a list of chats; each has a `mapping` of
message nodes and a `current_node`, and the chat as shown is the path from that node back to the
first one. Claude: `conversations.json` from the data export, a list of chats with
`chat_messages`, each message with a `sender` and text blocks.

A chat that does not have the expected shape is left out and counted, never guessed at; a file
that is not a list of chats, is too big or holds too much is refused whole.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from pi.core.errors import ServiceError

SOURCES = {"chatgpt": "ChatGPT", "claude": "Claude"}
MAX_BYTES = 128 * 1024 * 1024
MAX_CHATS = 20_000
MAX_MESSAGES = 500_000
MAX_CHAT_MESSAGES = 10_000
MAX_NODES = 50_000
MAX_PARTS = 1_000
MAX_ATTACHMENTS = 50
MAX_TEXT = 50_000
MAX_TITLE = 300
LATEST = 4102444800.0  # 2100-01-01: a later time is not a real chat time
EXPORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_SPACE = re.compile(r"[ \t\n\r]*")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
SKIP_REASONS = (
    "chatsNotMatching",
    "duplicateChats",
    "emptyChats",
    "longChats",
    "toolOrHiddenMessages",
    "emptyMessages",
    "duplicateMessages",
    "shortenedMessages",
)


class FormatRefused(ServiceError):
    """The file as a whole cannot be read as this kind of export."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message, 422)


class _Malformed(Exception):
    """One chat does not have the expected shape; it is left out and counted."""


@dataclass(frozen=True)
class Message:
    export_id: str
    author: str  # user | assistant: who wrote it in the other assistant
    text: str
    created_at: float | None
    shortened: bool


@dataclass(frozen=True)
class Chat:
    export_id: str
    title: str
    started_at: float | None
    updated_at: float | None
    messages: tuple[Message, ...]


@dataclass(frozen=True)
class Export:
    source: str
    chats: tuple[Chat, ...]
    skipped: dict[str, int]


def read(source: str, raw: bytes) -> Export:
    """Every chat the file holds in the expected shape, or a refusal that says why not."""
    if source not in SOURCES:
        raise FormatRefused("unknown_source", "Choose chatgpt or claude.")
    if len(raw) > MAX_BYTES:
        raise FormatRefused(
            "too_large", f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB."
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FormatRefused("not_json", "The file is not UTF-8 JSON.") from None
    skipped: Counter = Counter()
    chats, seen, total = [], set(), 0
    reader = _chatgpt if source == "chatgpt" else _claude
    other = None
    for index, item in enumerate(_items(text)):
        if index >= MAX_CHATS:
            raise FormatRefused("too_many_chats", f"The file holds more than {MAX_CHATS:,} chats.")
        try:
            chat = reader(item, skipped)
        except _Malformed:
            skipped["chatsNotMatching"] += 1
            other = other or _looks_like(item, source)
            continue
        if chat is None:
            continue
        if chat.export_id in seen:
            skipped["duplicateChats"] += 1
            continue
        seen.add(chat.export_id)
        if not chat.messages:
            skipped["emptyChats"] += 1
            continue
        total += len(chat.messages)
        if total > MAX_MESSAGES:
            raise FormatRefused(
                "too_many_messages", f"The file holds more than {MAX_MESSAGES:,} messages."
            )
        chats.append(chat)
    if not chats and skipped["chatsNotMatching"]:
        hint = f" It looks like a {SOURCES[other]} export." if other else ""
        raise FormatRefused(
            "wrong_format", f"No chat in this file matches the {SOURCES[source]} export.{hint}"
        )
    return Export(source, tuple(chats), {key: skipped[key] for key in SKIP_REASONS})


def _items(text: str):
    """The items of a top-level JSON list, one at a time, so one chat is in memory at once."""
    decoder = json.JSONDecoder(parse_constant=_no_constant)
    index = _SPACE.match(text, 0).end()
    if text[index : index + 1] != "[":
        raise FormatRefused("not_a_list", "The file is not a list of chats.")
    index = _SPACE.match(text, index + 1).end()
    if text[index : index + 1] == "]":
        index += 1
    else:
        while True:
            try:
                item, index = decoder.raw_decode(text, index)
            except (ValueError, RecursionError):
                raise FormatRefused("not_json", "The file is not valid JSON.") from None
            yield item
            index = _SPACE.match(text, index).end()
            mark = text[index : index + 1]
            index = _SPACE.match(text, index + 1).end()
            if mark == "]":
                break
            if mark != ",":
                raise FormatRefused("not_json", "The file is not valid JSON.")
    if _SPACE.match(text, index).end() != len(text):
        raise FormatRefused("not_json", "The file has more after its list of chats.")


def _no_constant(_name):
    raise ValueError("NaN and Infinity are not JSON.")


def _looks_like(item, source) -> str | None:
    if isinstance(item, dict):
        if source != "claude" and isinstance(item.get("chat_messages"), list):
            return "claude"
        if source != "chatgpt" and isinstance(item.get("mapping"), dict):
            return "chatgpt"
    return None


def _need(condition: bool) -> None:
    if not condition:
        raise _Malformed()


def _identity(value) -> str:
    _need(isinstance(value, str) and EXPORT_ID.fullmatch(value) is not None)
    return value


def _clean(value: str) -> str:
    """Text Pi can store and show: no NUL, no unpaired surrogate (which UTF-8 cannot hold)."""
    return _SURROGATE.sub("�", value.replace("\x00", ""))


def _title(value) -> str:
    _need(value is None or isinstance(value, str))
    return " ".join(_clean(value or "").split())[:MAX_TITLE]


def _seconds(value) -> float | None:
    if value is None:
        return None
    _need(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= LATEST)
    return float(value)


def _iso(value) -> float | None:
    if value is None:
        return None
    _need(isinstance(value, str) and len(value) <= 64)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise _Malformed() from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _seconds(moment.timestamp())


def _message(export_id, author, text, created_at, skipped) -> Message | None:
    text = _clean(text).strip()
    if not text:
        skipped["emptyMessages"] += 1
        return None
    shortened = len(text) > MAX_TEXT
    if shortened:
        skipped["shortenedMessages"] += 1
        text = (
            text[:MAX_TEXT] + f"\n\n[Shortened on import: {len(text) - MAX_TEXT:,} more "
            "characters were in the export.]"
        )
    return Message(export_id, author, text, created_at, shortened)


def _chat(export_id, title, started, updated, messages, skipped) -> Chat | None:
    if len(messages) > MAX_CHAT_MESSAGES:
        skipped["longChats"] += 1
        return None
    kept, seen = [], set()
    for message in messages:
        if message.export_id in seen:
            skipped["duplicateMessages"] += 1
            continue
        seen.add(message.export_id)
        kept.append(message)
    return Chat(export_id, title, started, updated, tuple(kept))


def _chatgpt(item, skipped) -> Chat | None:
    _need(isinstance(item, dict))
    export_id = _identity(item.get("conversation_id") or item.get("id"))
    title = _title(item.get("title"))
    started, updated = _seconds(item.get("create_time")), _seconds(item.get("update_time"))
    mapping = item.get("mapping")
    _need(isinstance(mapping, dict) and 0 < len(mapping) <= MAX_NODES)
    node_id, path, seen = item.get("current_node"), [], set()
    _need(isinstance(node_id, str) and node_id in mapping)
    while node_id is not None:
        _need(isinstance(node_id, str) and node_id not in seen and node_id in mapping)
        seen.add(node_id)
        node = mapping[node_id]
        _need(isinstance(node, dict))
        path.append((node_id, node))
        node_id = node.get("parent")
    messages = []
    for identity, node in reversed(path):
        if node.get("message") is None:
            continue
        message = _chatgpt_message(identity, node["message"], skipped)
        if message is not None:
            messages.append(message)
    return _chat(export_id, title, started, updated, messages, skipped)


def _chatgpt_message(node_id, message, skipped) -> Message | None:
    _need(isinstance(message, dict))
    author, content = message.get("author"), message.get("content")
    metadata = message.get("metadata") or {}
    _need(isinstance(author, dict) and isinstance(content, dict) and isinstance(metadata, dict))
    role, kind = author.get("role"), content.get("content_type")
    _need(isinstance(role, str) and isinstance(kind, str))
    export_id = _identity(message.get("id") or node_id)
    created = _seconds(message.get("create_time"))
    if (
        role not in ("user", "assistant")
        or kind not in ("text", "multimodal_text")
        or message.get("recipient", "all") != "all"
        or metadata.get("is_visually_hidden_from_conversation") is True
    ):
        # Tool calls and results, browsing, code runs, reasoning, custom instructions and the
        # other assistant's own memory notes: not what either side said in the chat.
        skipped["toolOrHiddenMessages"] += 1
        return None
    parts = content.get("parts")
    _need(isinstance(parts, list) and len(parts) <= MAX_PARTS)
    pieces = []
    for part in parts:
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, dict):
            # An uploaded image or file: only a mark that one was there, never its bytes.
            pieces.append("[image]" if "image" in str(part.get("content_type", "")) else "[file]")
        else:
            _need(part is None)
    return _message(export_id, role, "\n".join(pieces), created, skipped)


def _claude(item, skipped) -> Chat | None:
    _need(isinstance(item, dict))
    export_id = _identity(item.get("uuid"))
    title = _title(item.get("name"))
    started, updated = _iso(item.get("created_at")), _iso(item.get("updated_at"))
    entries = item.get("chat_messages")
    _need(isinstance(entries, list))
    if len(entries) > MAX_CHAT_MESSAGES:
        skipped["longChats"] += 1
        return None
    messages = []
    for entry in entries:
        message = _claude_message(entry, skipped)
        if message is not None:
            messages.append(message)
    return _chat(export_id, title, started, updated, messages, skipped)


def _claude_message(entry, skipped) -> Message | None:
    _need(isinstance(entry, dict))
    export_id = _identity(entry.get("uuid"))
    sender = entry.get("sender")
    _need(sender in ("human", "assistant"))
    created = _iso(entry.get("created_at"))
    blocks = entry.get("content")
    if blocks is not None and blocks != []:
        _need(isinstance(blocks, list) and len(blocks) <= MAX_PARTS)
        pieces = []
        for block in blocks:
            _need(isinstance(block, dict) and isinstance(block.get("type"), str))
            if block["type"] == "text":
                _need(isinstance(block.get("text"), str))
                pieces.append(block["text"])
        if not pieces:
            # Only tool use, tool results or thinking: nothing either side said.
            skipped["toolOrHiddenMessages"] += 1
            return None
        text = "\n\n".join(pieces)
    else:
        text = entry.get("text")
        _need(text is None or isinstance(text, str))
        text = text or ""
    names = []
    for key in ("attachments", "files"):
        listed = entry.get(key) or []
        _need(isinstance(listed, list) and len(listed) <= MAX_ATTACHMENTS)
        for attachment in listed:
            _need(isinstance(attachment, dict))
            name = attachment.get("file_name")
            _need(name is None or isinstance(name, str))
            if name and name not in names:
                names.append(name)
    # The names only: an attachment's extracted text is not part of what was said.
    marks = "\n".join(f"[file: {' '.join(name.split())[:200]}]" for name in names)
    text = f"{text}\n\n{marks}" if text.strip() and marks else text or marks
    return _message(export_id, "user" if sender == "human" else "assistant", text, created, skipped)
