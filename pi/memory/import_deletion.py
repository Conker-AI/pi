"""Deleting one imported chat for good: its messages, its memory offers, and the chat itself.

The rows go in one transaction with SQLite's secure delete on, and the write-ahead log is folded
back into the database after, so the words do not stay in the file's free pages. What is kept is
a short record with ids and counts only (no title, no text): it answers a repeated request and
makes a later import of the same export skip this chat, so a deleted chat does not come back.

Memories MemoryGate already made from the owner's words in this chat are not touched here: the
reply counts them, and the owner forgets them in Memory. The background sender holds its lock for
each offer, and the deletion takes the same lock, so no word of a chat is sent while or after it
is deleted.
"""

from __future__ import annotations

import contextlib
import json
import time

from pi.memory.imports import LABELS, ImportRefused

MEMORY_NOTE = "Memories made from your words in this chat stay until you forget them in Memory."


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


def delete_chat(store, chat_id: str, *, sending=None) -> dict:
    """Delete one imported chat. Repeating the request answers with the same record."""
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
