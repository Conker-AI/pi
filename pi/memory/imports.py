"""Imported chats from ChatGPT and Claude: an archive the owner reads, never a chat Conker is in.

Each chat in an export becomes one archived, read-only chat, labelled with where it came from and
keeping the export's own ids and times. They live in their own tables, apart from the
conversation store: no turn, fork, search, summary or recall reads them, so imported words can
never stand for the owner's own message in an access decision. Nothing runs during an import: no
model, no tool, no Conker.

The messages the owner wrote in the other assistant are offered to MemoryGate as an outside
source (import_offers), never as owner statements; the other assistant's replies stay in the
archive only. Importing the same export again adds nothing: a chat is known by the export's chat
id and a message by its message id, so a later export of a chat that went on adds only the new
messages.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from pi.conversation import chat_preferences
from pi.core.errors import ServiceError
from pi.memory import import_formats

LABELS = {source: f"Imported from {name}" for source, name in import_formats.SOURCES.items()}
# MemoryGate keeps at most this much text per piece of conversation evidence.
MAX_OFFER = 16000
SAMPLE_TITLES = 5
UNTITLED = "Untitled chat"

SCHEMA = """
CREATE TABLE IF NOT EXISTS imported_chats (
 id TEXT PRIMARY KEY,
 source TEXT NOT NULL CHECK(source IN ('chatgpt','claude')),
 export_id TEXT NOT NULL, title TEXT NOT NULL,
 started_at REAL, updated_at REAL, imported_at REAL NOT NULL, refreshed_at REAL NOT NULL,
 UNIQUE(source, export_id)
);
CREATE TABLE IF NOT EXISTS imported_messages (
 id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES imported_chats(id),
 seq INTEGER NOT NULL, export_id TEXT NOT NULL,
 author TEXT NOT NULL CHECK(author IN ('user','assistant')),
 text TEXT NOT NULL, created_at REAL, shortened INTEGER NOT NULL DEFAULT 0,
 UNIQUE(chat_id, export_id), UNIQUE(chat_id, seq)
);
CREATE TRIGGER IF NOT EXISTS imported_messages_are_kept BEFORE UPDATE ON imported_messages
BEGIN SELECT RAISE(ABORT, 'imported messages stay as they were imported'); END;
CREATE TABLE IF NOT EXISTS import_batches (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL, bytes INTEGER NOT NULL,
 imported_at REAL NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_memory_offers (
 message_id TEXT PRIMARY KEY REFERENCES imported_messages(id),
 state TEXT NOT NULL CHECK(state IN ('pending','sent','refused')),
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', evidence_id TEXT, sent_at REAL
);
CREATE INDEX IF NOT EXISTS import_memory_offers_waiting
 ON import_memory_offers(next_at) WHERE state='pending';
"""


class ImportRefused(ServiceError):
    """An import request Pi cannot carry out, in the API's one refusal shape."""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def memory_on(db) -> tuple[bool, str | None]:
    """Whether imported words may be offered to memory, by the owner's own settings: not when he
    skipped memory at setup, nor when new chats start private (an imported chat is a new chat)."""
    choice = db.execute(
        "SELECT choice FROM setup_optional_choices WHERE step='memory' "
        "ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    if choice is not None and choice[0] == "skip":
        return False, "Memory was skipped at setup, so nothing is offered to memory."
    if chat_preferences.new_chat_privacy(db)["memoryDisabled"]:
        return False, "New chats are private in Settings, so nothing is offered to memory."
    return True, None


def offerable(message: import_formats.Message) -> bool:
    """Only what the owner wrote, whole: a shortened or very long message is archived only."""
    return message.author == "user" and not message.shortened and len(message.text) <= MAX_OFFER


def _known(db, source, chat):
    row = db.execute(
        "SELECT id FROM imported_chats WHERE source=? AND export_id=?", (source, chat.export_id)
    ).fetchone()
    if row is None:
        return None, list(chat.messages)
    seen = {
        item[0]
        for item in db.execute("SELECT export_id FROM imported_messages WHERE chat_id=?", (row[0],))
    }
    return row[0], [message for message in chat.messages if message.export_id not in seen]


def _times(export):
    for chat in export.chats:
        yield chat.started_at
        yield chat.updated_at
        for message in chat.messages:
            yield message.created_at


def _memory(db, configured):
    on, reason = memory_on(db)
    return {"on": on, "reason": reason, "configured": configured}


def preview(store, source: str, raw: bytes, *, memory_configured: bool) -> dict:
    """What importing this file would add, read without saving anything."""
    export = import_formats.read(source, raw)
    with store._connect() as db:
        db.execute("BEGIN")
        plans = [(chat, *_known(db, source, chat)) for chat in export.chats]
        memory = _memory(db, memory_configured)
    fresh = [message for _, _, new in plans for message in new]
    new_chats = sum(1 for _, row, _ in plans if row is None)
    updated = sum(1 for _, row, new in plans if row is not None and new)
    changed = sorted(
        (chat for chat, row, new in plans if row is None or new),
        key=lambda chat: chat.updated_at or chat.started_at or 0,
        reverse=True,
    )
    times = [moment for moment in _times(export) if moment is not None]
    return {
        "schemaVersion": 1,
        "source": source,
        "label": LABELS[source],
        "digest": digest(raw),
        "bytes": len(raw),
        "chats": {
            "inFile": len(export.chats),
            "new": new_chats,
            "updated": updated,
            "unchanged": len(plans) - new_chats - updated,
        },
        "messages": {
            "inFile": sum(len(chat.messages) for chat in export.chats),
            "new": len(fresh),
            "yours": sum(1 for message in fresh if message.author == "user"),
            "offeredToMemory": sum(1 for message in fresh if offerable(message))
            if memory["on"]
            else 0,
        },
        "firstAt": min(times) if times else None,
        "lastAt": max(times) if times else None,
        "sampleTitles": [chat.title or UNTITLED for chat in changed[:SAMPLE_TITLES]],
        "skipped": export.skipped,
        "memory": memory,
        "limits": {
            "bytes": import_formats.MAX_BYTES,
            "chats": import_formats.MAX_CHATS,
            "messages": import_formats.MAX_MESSAGES,
        },
    }


def save(store, source: str, raw: bytes, expected: str, *, memory_configured: bool) -> dict:
    """Import exactly the file the owner previewed (named by its digest); each chat is saved
    whole or not at all, so a stopped import is finished by importing the same file again."""
    if digest(raw) != expected:
        raise ImportRefused(
            "file_changed", "This is not the file you previewed. Preview it again, then import."
        )
    export = import_formats.read(source, raw)
    counts = {"new": 0, "updated": 0, "unchanged": 0, "added": 0, "offered": 0}
    now = time.time()
    with store._connect() as db:
        memory = _memory(db, memory_configured)
        for chat in export.chats:
            db.execute("BEGIN IMMEDIATE")
            chat_id, fresh = _known(db, source, chat)
            if not fresh:
                db.execute("ROLLBACK")
                counts["unchanged"] += 1
                continue
            if chat_id is None:
                chat_id = "imp_" + uuid.uuid4().hex
                db.execute(
                    "INSERT INTO imported_chats VALUES (?,?,?,?,?,?,?,?)",
                    (
                        chat_id,
                        source,
                        chat.export_id,
                        chat.title,
                        chat.started_at,
                        chat.updated_at,
                        now,
                        now,
                    ),
                )
                counts["new"] += 1
            else:
                db.execute(
                    "UPDATE imported_chats SET title=?,updated_at=COALESCE(?,updated_at),"
                    "refreshed_at=? WHERE id=?",
                    (chat.title, chat.updated_at, now, chat_id),
                )
                counts["updated"] += 1
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM imported_messages WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            for message in fresh:
                seq += 1
                message_id = "impmsg_" + uuid.uuid4().hex
                db.execute(
                    "INSERT INTO imported_messages VALUES (?,?,?,?,?,?,?,?)",
                    (
                        message_id,
                        chat_id,
                        seq,
                        message.export_id,
                        message.author,
                        message.text,
                        message.created_at,
                        int(message.shortened),
                    ),
                )
                counts["added"] += 1
                if memory["on"] and offerable(message):
                    db.execute(
                        "INSERT INTO import_memory_offers(message_id,state) VALUES (?, 'pending')",
                        (message_id,),
                    )
                    counts["offered"] += 1
            db.commit()
        result = {
            "schemaVersion": 1,
            "batchId": "impb_" + uuid.uuid4().hex,
            "source": source,
            "label": LABELS[source],
            "digest": expected,
            "importedAt": now,
            "chats": {key: counts[key] for key in ("new", "updated", "unchanged")},
            "messages": {"added": counts["added"], "offeredToMemory": counts["offered"]},
            "skipped": export.skipped,
            "memory": memory,
        }
        db.execute(
            "INSERT INTO import_batches VALUES (?,?,?,?,?,?)",
            (result["batchId"], source, expected, len(raw), now, json.dumps(result)),
        )
    return result


def batches(store, limit: int = 50) -> list[dict]:
    """The latest imports, newest first, as their results were reported."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT summary FROM import_batches ORDER BY imported_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _chat_view(row) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "label": LABELS[row["source"]],
        "title": row["title"],
        "exportId": row["export_id"],
        "startedAt": row["started_at"],
        "updatedAt": row["updated_at"],
        "importedAt": row["imported_at"],
        "messageCount": row["messages"],
        "archived": True,
        "readOnly": True,
    }


_CHAT = (
    "SELECT c.*, (SELECT COUNT(*) FROM imported_messages m WHERE m.chat_id=c.id) AS messages "
    "FROM imported_chats c"
)


def chats(store, *, source: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """Imported chats, the latest first, a page at a time."""
    where, values = ("WHERE c.source=?", [source]) if source else ("", [])
    with store._connect() as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) FROM imported_chats c " + where, values).fetchone()[0]
        rows = db.execute(
            f"{_CHAT} {where} ORDER BY COALESCE(c.updated_at, c.started_at, c.imported_at) DESC,"
            " c.id DESC LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
    return {
        "schemaVersion": 1,
        "total": total,
        "chats": [_chat_view(row) for row in rows],
        "nextOffset": offset + len(rows) if offset + len(rows) < total else None,
    }


def chat(store, chat_id: str, *, after: int = 0, limit: int = 100) -> dict:
    """One imported chat and a page of its messages, in the order they were shown."""
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute(_CHAT + " WHERE c.id=?", (chat_id,)).fetchone()
        if row is None:
            raise ImportRefused("not_found", "There is no imported chat with that id.", 404)
        messages = db.execute(
            "SELECT m.*, o.state AS offer FROM imported_messages m "
            "LEFT JOIN import_memory_offers o ON o.message_id=m.id "
            "WHERE m.chat_id=? AND m.seq>? ORDER BY m.seq LIMIT ?",
            (chat_id, after, limit + 1),
        ).fetchall()
    page = messages[:limit]
    return {
        "schemaVersion": 1,
        **_chat_view(row),
        "messages": [
            {
                "id": message["id"],
                "seq": message["seq"],
                "author": message["author"],
                "text": message["text"],
                "createdAt": message["created_at"],
                "shortened": bool(message["shortened"]),
                "exportId": message["export_id"],
                # Only his own messages are offered to memory; replies never are.
                "memory": None
                if message["author"] != "user"
                else {"pending": "offering", "sent": "offered", "refused": "refused"}.get(
                    message["offer"], "not_offered"
                ),
            }
            for message in page
        ],
        "nextAfter": page[-1]["seq"] if len(messages) > limit else None,
    }
