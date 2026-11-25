"""The owner settles, by hand, a deleted imported chat's erasure that no listener will finish.

Words an older Pi sent to MemoryGate without recording which import listener got them stay
counted as erasing once the listener set now answers not found, since MemoryGate gives that answer
for another listener's event too (imports.ERASING). No listener Pi knows erases them, so the Inbox
card stays open for good (import_offers.erasure_health). The owner may settle one such deleted
chat's erasure, with the password (the gateway asks for it, as for any forget):

- refused while a listener set now can still erase a word of it (imports.REACHABLE, what the
  sender tries), while a word waits for a listener the owner can set up or set back, and when
  nothing of the chat is left to erase;
- Pi clears its own saved copies as a memory forget does (`invalidate_recall`: saved recall, saved
  `conker.recall` results, captured model inputs; a recall in flight is not kept), in the
  transaction that records the settlement, with secure delete on and the log folded back after;
- the erasure rows stay, so every reader keeps the words, and what MemoryGate may have made from
  them, hidden for good (pi.conversation.hidden_records); only the card and the counts stop
  (imports.OPEN);
- MemoryGate is not asked, and the record says nothing there was erased: a copy may remain where
  no listener reached. A listener configured later still tries each word once
  (import_deletion.erase_waiting), and one that holds them erases them then.

The record keeps ids and counts only. A repeat answers with the same record; two at once settle
it once (one write transaction).
"""

from __future__ import annotations

import time

from pi.memory import import_offers
from pi.memory import store as memory_store
from pi.memory.imports import ERASING, LABELS, REACHABLE, ImportRefused

NOTE = (
    "Settled by hand. Nothing was erased in MemoryGate: no import listener Conker can reach had "
    "these words, so a copy may remain where none reached. Conker removed its own saved copies "
    "and keeps the words out of recall and search for good, with every memory made from "
    "imported chats, since it cannot tell which came from this one."
)
REFUSALS = {
    "listener_reachable": "The import listener set up now can still erase these words, and Conker "
    "keeps trying. Wait for it; settle by hand only once it did not find them.",
    "not_stuck": "Nothing of this chat is stuck: its words wait for an import listener you can "
    "set up or set back (the Inbox card says which), or nothing is left to erase.",
}
# While the listener set now refuses Pi (401, 403: its key; 404: its source), waiting does not
# help: the refusal names what to fix there first (import_offers.FIXES, as the Inbox card does).
REFUSED_FIX = {401: "key", 403: "key", 404: "source"}
THEN = " Then Conker tries these words again; settle by hand only if it does not find them."
# Which erasures the list shows: not settled yet (the default), settled by hand, or both.
SHOWN = {"open": "s.chat_id IS NULL", "settled": "s.chat_id IS NOT NULL", "all": "1"}
LIST_LIMIT = 100


def _held(db, chat_id: str, listener: str | None) -> tuple[str, int, int | None]:
    """This deleted chat's erasure: `stuck` (it may be settled) or why not, how many of its
    words MemoryGate may still hold, and the refusal of the listener set now (`listener`, None
    for none) for a word it still tries, if any."""
    held, pending = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(state='pending'), 0) FROM import_evidence_erasures "
        f"WHERE chat_id=? AND {ERASING}",
        (chat_id,),
    ).fetchone()
    # With no listener set up, the sender tries nothing (REACHABLE would match NULL keys).
    reachable, refused = (
        (0, None)
        if listener is None
        else db.execute(
            "SELECT COUNT(*), MAX(refused) FROM import_evidence_erasures "
            f"WHERE chat_id=? AND {REACHABLE}",
            (chat_id, listener, listener),
        ).fetchone()
    )
    if reachable:
        return "listener_reachable", held, refused
    if pending or not held:
        return "not_stuck", held, None
    return "stuck", held, None


def _message(state: str, refused: int | None) -> str:
    fix = REFUSED_FIX.get(refused) if state == "listener_reachable" and refused else None
    return import_offers.FIXES[fix] + THEN if fix else REFUSALS[state]


def _settlement(db, chat_id: str):
    return db.execute(
        "SELECT * FROM import_erasure_settlements WHERE chat_id=?", (chat_id,)
    ).fetchone()


def _receipt(deletion, settled, scrubbed: bool | None) -> dict:
    return {
        "schemaVersion": 1,
        "deletionId": deletion["id"],
        "source": deletion["source"],
        "label": LABELS[deletion["source"]],
        "state": "settled_by_owner",
        "deletedAt": deletion["deleted_at"],
        "settledAt": settled["settled_at"],
        "wordsSettled": settled["words"],
        "savedRecallCleared": settled["cleared"],
        "note": NOTE,
        "fileScrubbed": scrubbed,
    }


def settle(store, chat_id: str, *, listener: str | None, clock=time.time) -> dict:
    """Settle one deleted imported chat's stuck erasure (see the module docstring)."""
    with store._connect() as db:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            deletion = db.execute(
                "SELECT * FROM imported_chat_deletions WHERE id=?", (chat_id,)
            ).fetchone()
            if deletion is None:
                raise ImportRefused(
                    "not_found", "There is no deleted imported chat with that id.", 404
                )
            prior = _settlement(db, chat_id)
            if prior is not None:
                db.rollback()
                return _receipt(deletion, prior, None)
            state, held, refused = _held(db, chat_id, listener)
            if state != "stuck":
                raise ImportRefused(state, _message(state, refused), 409)
            # Recall leaves the words out from now on as before; Pi's saved copies go now.
            cleared = memory_store.invalidate_recall(db)
            db.execute(
                "INSERT INTO import_erasure_settlements VALUES (?,?,?,?)",
                (chat_id, clock(), held, cleared),
            )
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        settled = _settlement(db, chat_id)
        busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    return _receipt(deletion, settled, not busy)


def erasures(
    store, *, listener: str | None, state: str = "open", limit: int = 50, offset: int = 0
) -> dict:
    """Deleted imported chats whose words MemoryGate may still hold, the latest first, a page at
    a time: whether each may be settled now, or why not. `state` (SHOWN): those not settled yet,
    the ones the owner settled, or all. Ids and counts only."""
    where = (
        "WHERE (s.chat_id IS NOT NULL OR EXISTS (SELECT 1 FROM import_evidence_erasures e "
        f"WHERE e.chat_id=d.id AND {ERASING})) AND {SHOWN[state]}"
    )
    joined = (
        "FROM imported_chat_deletions d LEFT JOIN import_erasure_settlements s ON s.chat_id=d.id "
    )
    with store._connect() as db:
        db.execute("BEGIN")
        total = db.execute(f"SELECT COUNT(*) {joined}{where}").fetchone()[0]
        rows = db.execute(
            f"SELECT d.id, d.source, d.deleted_at, s.settled_at, s.words {joined}{where} "
            "ORDER BY d.deleted_at DESC, d.id DESC LIMIT ? OFFSET ?",
            (min(limit, LIST_LIMIT), offset),
        ).fetchall()
        items = []
        for row in rows:
            if row["settled_at"] is not None:
                shown, words, refusal = "settled_by_owner", row["words"], None
            else:
                held, words, refused = _held(db, row["id"], listener)
                refusal = None if held == "stuck" else (held, _message(held, refused))
                shown = "stuck" if refusal is None else "erasing"
            items.append(
                {
                    "deletionId": row["id"],
                    "source": row["source"],
                    "label": LABELS[row["source"]],
                    "deletedAt": row["deleted_at"],
                    "words": words,
                    "state": shown,
                    "refusal": {"code": refusal[0], "message": refusal[1]} if refusal else None,
                    "settledAt": row["settled_at"],
                }
            )
    return {
        "schemaVersion": 1,
        "total": total,
        "erasures": items,
        "nextOffset": offset + len(rows) if offset + len(rows) < total else None,
        "note": NOTE,
    }
