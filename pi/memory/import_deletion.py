"""Deleting one imported chat for good: its messages, its memory offers, and the chat itself.

The rows go in one transaction with SQLite's secure delete on, and the write-ahead log is folded
back into the database after, so the words do not stay in the file's free pages. What is kept is
a short record with ids and counts only (no title, no text): it answers a repeated request and
makes a later import of the same export skip this chat, so a deleted chat does not come back.

Each word of the chat that already went to MemoryGate is erased there too, with what MemoryGate
made from it (analyses, the memory it alone supports): the deletion queues the evidence ids in the
same transaction, and the background sender erases them before it offers anything new, retrying
until MemoryGate answers (`erase_waiting`). The sender holds its lock for each offer, and the
deletion takes the same lock, so no word of a chat is sent while or after it is deleted.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass

import httpx

from pi.memory.imports import LABELS, ImportRefused

MEMORY_NOTE = (
    "Your words from this chat are being erased from memory too, with the memories made from "
    "them alone."
)
MAX_BACKOFF_SECONDS = 300
# MemoryGate's evidence ids are UUIDs; anything else could not be one path segment.
EVIDENCE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


def _receipt(row, scrubbed: bool | None) -> dict:
    return {
        "schemaVersion": 1,
        "chatId": row["id"],
        "source": row["source"],
        "label": LABELS[row["source"]],
        "deletedAt": row["deleted_at"],
        "messagesDeleted": row["messages"],
        "offeredToMemory": row["offered"],
        "memoryNote": MEMORY_NOTE if row["offered"] else None,
        "fileScrubbed": scrubbed,
    }


def delete_chat(store, chat_id: str, *, sending=None, source_key: str | None = None) -> dict:
    """Delete one imported chat. Repeating the request answers with the same record.

    `source_key` is the listener the words went to (the configured one); its erasures go only
    through that listener, so a listener changed later cannot answer for them."""
    with sending if sending is not None else contextlib.nullcontext(), store._connect() as db:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            chat = db.execute(
                "SELECT source, export_id FROM imported_chats WHERE id=?", (chat_id,)
            ).fetchone()
            if chat is None:
                prior = db.execute(
                    "SELECT * FROM imported_chat_deletions WHERE id=?", (chat_id,)
                ).fetchone()
                db.rollback()
                if prior is None:
                    raise ImportRefused("not_found", "Imported chat not found.", 404)
                return _receipt(prior, None)
            messages = db.execute(
                "SELECT COUNT(*) FROM imported_messages WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            evidence = [
                row[0]
                for row in db.execute(
                    "SELECT o.evidence_id FROM import_memory_offers o JOIN imported_messages m "
                    "ON m.id=o.message_id WHERE m.chat_id=? AND o.state='sent' "
                    "AND o.evidence_id IS NOT NULL ORDER BY o.evidence_id",
                    (chat_id,),
                )
            ]
            db.executemany(
                "INSERT OR IGNORE INTO import_evidence_erasures(evidence_id, chat_id, source_key) "
                "VALUES (?,?,?)",
                [(evidence_id, chat_id, source_key) for evidence_id in evidence],
            )
            db.execute(
                "DELETE FROM import_memory_offers WHERE message_id IN "
                "(SELECT id FROM imported_messages WHERE chat_id=?)",
                (chat_id,),
            )
            db.execute("DELETE FROM imported_messages WHERE chat_id=?", (chat_id,))
            db.execute("DELETE FROM imported_chats WHERE id=?", (chat_id,))
            db.execute(
                "INSERT INTO imported_chat_deletions VALUES (?,?,?,?,?,?,?)",
                (
                    chat_id,
                    chat["source"],
                    chat["export_id"],
                    time.time(),
                    messages,
                    len(evidence),
                    json.dumps(evidence),
                ),
            )
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        row = db.execute("SELECT * FROM imported_chat_deletions WHERE id=?", (chat_id,)).fetchone()
        busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    return _receipt(row, not busy)


@dataclass(frozen=True)
class Erased:
    done: int
    paused: int | None = None  # the HTTP code when the listener itself refused
    problem: str | None = None


def erase_waiting(store, client, clock=time.time, limit: int = 20) -> Erased:
    """Erase waiting evidence of deleted imported chats in MemoryGate, oldest first.

    An event MemoryGate does not have (a restore from before it was sent) is kept as missing,
    so the count shows it; a refusal of the listener itself pauses the sender as for offers.
    Erasures recorded for another listener wait, named in the problem, until it is set back."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT evidence_id, attempts FROM import_evidence_erasures "
            "WHERE state='pending' AND next_at<=? AND (source_key IS NULL OR source_key=?) "
            "ORDER BY next_at, evidence_id LIMIT ?",
            (clock(), client.source_key, limit),
        ).fetchall()
        elsewhere = db.execute(
            "SELECT COUNT(*) FROM import_evidence_erasures WHERE state='pending' "
            "AND source_key IS NOT NULL AND source_key<>?",
            (client.source_key,),
        ).fetchone()[0]
    done = 0
    problem = (
        f"{elsewhere} words of deleted imported chats went to another import listener; set "
        "PI_MEMORYGATE_IMPORT_SOURCE back to it to erase them."
        if elsewhere
        else None
    )
    for evidence_id, attempts in rows:
        if not EVIDENCE_ID.fullmatch(evidence_id):
            _mark_missing(store, evidence_id, "not an evidence id")
            continue
        try:
            found = client.erase(evidence_id)
        except (httpx.HTTPError, ValueError, TypeError, RecursionError) as exc:
            code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if code in (401, 403, 404, 429):
                return Erased(done, paused=code)
            problem = "MemoryGate did not erase a deleted chat's words yet; this is retried."
            with store._connect() as db:
                db.execute(
                    "UPDATE import_evidence_erasures SET attempts=attempts+1, next_at=?, error=? "
                    "WHERE evidence_id=?",
                    (
                        clock() + min(MAX_BACKOFF_SECONDS, 2 ** min(attempts + 1, 9)),
                        f"HTTP {code}" if code else type(exc).__name__,
                        evidence_id,
                    ),
                )
            continue
        if not found:
            _mark_missing(store, evidence_id, "not found")
        else:
            with store._connect() as db:
                db.execute(
                    "DELETE FROM import_evidence_erasures WHERE evidence_id=?", (evidence_id,)
                )
        done += 1
    return Erased(done, problem=problem)


def _mark_missing(store, evidence_id: str, reason: str) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE import_evidence_erasures SET state='missing', error=? WHERE evidence_id=?",
            (reason, evidence_id),
        )
