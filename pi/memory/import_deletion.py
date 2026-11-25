"""Deleting one imported chat for good: its messages, its memory offers, and the chat itself.

The rows go in one transaction with SQLite's secure delete on, and the write-ahead log is folded
back into the database after, so the words do not stay in the file's free pages. What is kept is
a short record with ids and counts only (no title, no text): it answers a repeated request and
makes a later import of the same export skip this chat, so a deleted chat does not come back.

Each word of the chat that already went to MemoryGate is erased there too, with what MemoryGate
made from it (analyses, the memory it alone supports): the deletion queues the evidence ids in the
same transaction, and the background sender erases them before it offers anything new, retrying
until MemoryGate answers (`erase_waiting`). The sender holds its lock for each offer, and the
deletion takes the same lock, so no word of a chat is sent while or after it is deleted. Until an
erasure settles, recall and every saved recall leave out what may hold those words
(pi.conversation.hidden_records), and one still waiting after three days, or refused by the
listener, is an Inbox card (`import_offers.erasure_health`).
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass

import httpx

from pi.memory import store as memory_store
from pi.memory.imports import LABELS, REACHABLE, ImportRefused

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

    Each word's erasure goes only through the listener it was offered to (recorded with the
    offer), so another listener cannot answer for it. `source_key`, the configured listener,
    stands in for a word offered before Pi recorded that; such a word stays counted as erasing
    if MemoryGate does not find it (imports.ERASING)."""
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
            offered = db.execute(
                "SELECT o.evidence_id, o.source_key FROM import_memory_offers o "
                "JOIN imported_messages m ON m.id=o.message_id WHERE m.chat_id=? "
                "AND o.state='sent' AND o.evidence_id IS NOT NULL ORDER BY o.evidence_id",
                (chat_id,),
            ).fetchall()
            evidence = [row[0] for row in offered]
            db.executemany(
                "INSERT OR IGNORE INTO import_evidence_erasures"
                "(evidence_id, chat_id, source_key, offered_via) VALUES (?,?,?,?)",
                [(row[0], chat_id, row[1] or source_key, row[1]) for row in offered],
            )
            if evidence:
                # Recall leaves these words out from now on; one retrieved before is not kept
                # (the recall generation, pi.memory.store).
                db.execute("UPDATE recall_generation SET value=value+1 WHERE id=1")
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
    so the count shows it; a refusal of the listener itself pauses the sender as for offers and
    is kept on the row (`refused`). Erasures recorded for another listener wait, named in the
    problem, until it is set back. One a listener did not find, offered before Pi recorded where,
    is tried once more through each other listener configured later, also once the owner settled
    it by hand (import_settle): one that still holds the words erases them then."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT evidence_id, attempts FROM import_evidence_erasures WHERE next_at<=? AND "
            f"{REACHABLE} ORDER BY next_at, evidence_id LIMIT ?",
            (clock(), client.source_key, client.source_key, limit),
        ).fetchall()
        elsewhere = db.execute(
            "SELECT COUNT(*) FROM import_evidence_erasures WHERE state='pending' "
            "AND source_key IS NOT NULL AND source_key<>?",
            (client.source_key,),
        ).fetchone()[0]
    problem = (
        f"{elsewhere} words of deleted imported chats went to another import listener; set "
        "PI_MEMORYGATE_IMPORT_SOURCE back to it to erase them."
        if elsewhere
        else None
    )
    settled: list[tuple[str, bool]] = []  # (evidence id, MemoryGate still had it)
    try:
        return _erase(store, client, clock, rows, settled, problem)
    finally:
        # After the calls, never between them: one clearing of saved recall per batch.
        _settled(store, settled, client.source_key)


def _erase(store, client, clock, rows, settled, problem) -> Erased:
    """MemoryGate's calls for one batch; what it no longer holds goes into `settled`."""
    for evidence_id, attempts in rows:
        if not EVIDENCE_ID.fullmatch(evidence_id):
            _mark_missing(store, evidence_id, "not an evidence id", client.source_key)
            continue
        try:
            found = client.erase(evidence_id)
        except (httpx.HTTPError, ValueError, TypeError, RecursionError) as exc:
            code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if code in (401, 403, 404, 429):
                if code != 429:  # a lockout follows refusals; the refusal is what to fix
                    with store._connect() as db:
                        db.execute(
                            "UPDATE import_evidence_erasures SET refused=? WHERE evidence_id=?",
                            (code, evidence_id),
                        )
                return Erased(len(settled), paused=code)
            problem = "MemoryGate did not erase a deleted chat's words yet; this is retried."
            with store._connect() as db:
                db.execute(
                    "UPDATE import_evidence_erasures SET attempts=attempts+1, next_at=?, error=?, "
                    "refused=NULL WHERE evidence_id=?",
                    (
                        clock() + min(MAX_BACKOFF_SECONDS, 2 ** min(attempts + 1, 9)),
                        f"HTTP {code}" if code else type(exc).__name__,
                        evidence_id,
                    ),
                )
            continue
        settled.append((evidence_id, found))
    return Erased(len(settled), problem=problem)


def _settled(store, settled: list[tuple[str, bool]], listener: str) -> None:
    """Events `listener` holds nothing of now: erased by this batch or, when not found, by an
    earlier call whose answer was lost (or, for one Pi does not know was offered to it, under
    another listener: it stays counted as erasing, imports.ERASING, and records who answered).
    What they alone supported may be in Pi's saved recall, so
    it is cleared once, as a memory forget clears it (`invalidate_recall`, which also stops a recall
    in flight from being saved), in the transaction that records them: a crash before it commits
    leaves them pending, and the next try finds them gone and clears then. Recall is not paused
    while erasures wait: MemoryGate holds the words until then."""
    if not settled:
        return
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany(
            "DELETE FROM import_evidence_erasures WHERE evidence_id=?",
            [(evidence_id,) for evidence_id, found in settled if found],
        )
        db.executemany(
            "UPDATE import_evidence_erasures SET state='missing', error='not found', "
            "source_key=?, refused=NULL WHERE evidence_id=?",
            [(listener, evidence_id) for evidence_id, found in settled if not found],
        )
        memory_store.invalidate_recall(db)
        db.commit()


def _mark_missing(store, evidence_id: str, reason: str, listener: str) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE import_evidence_erasures SET state='missing', error=?, source_key=? "
            "WHERE evidence_id=?",
            (reason, listener, evidence_id),
        )
