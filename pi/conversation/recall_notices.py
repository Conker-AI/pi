"""Inbox notice "answers used a memory you changed" (R-U3 step 3).

When a memory correction or a forget finishes, Pi files one notice if earlier answers used that
memory (`pi.memory.uses`). The notice holds ids only: the memory id, whether it was corrected or
forgotten, the revision the old answers saw and the request id that is its key. It holds no
memory words and no chat id. How many answers and chats it reaches is counted when the Inbox is
read, from `memory_uses` rows in chats that are not forgotten and not in the Trash, so a chat
that leaves takes itself out of the count, and a notice that reaches nothing is not shown.

Design: context/design/memory-recall-notice.md in the Conker workspace.
"""

from __future__ import annotations

import re
import time
import uuid

from pi.conversation import visibility
from pi.core.errors import ServiceError
from pi.owner import notices

NOTICE_ID = r"^mrn_[0-9a-f]{32}$"
CHANGES = ("corrected", "forgotten")
MAX_CHATS = 20
MAX_OPEN = 100  # notices read per Inbox call, newest first
STORED_TITLE = "Memory changed"  # the card's words are built when read, from visible chats
_VISIBLE = f"s.status<>'forgotten' AND NOT {visibility.hidden_sql('u.session_id')}"


class RecallNoticeError(ServiceError):
    pass


def _identity(request_id: str) -> str:
    return "mrn_" + uuid.uuid5(uuid.NAMESPACE_URL, f"pi-memory-recall:{request_id}").hex


def _reach(db, memory_id: str, revision: int, until: float):
    """Uses of the memory as it was (up to `revision`, before `until`) in visible chats."""
    return db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT u.session_id) FROM memory_uses u "
        "JOIN sessions s ON s.id=u.session_id "
        f"WHERE u.memory_id=? AND u.recorded_at<=? AND (u.revision IS NULL OR u.revision<=?) "
        f"AND {_VISIBLE}",
        (memory_id, until, revision),
    ).fetchone()


def file(
    db, *, request_id: str, memory_id: str, change: str, revision: int, now: float | None = None
) -> bool:
    """Note that a change finished; False when no answer used the memory or it is already filed.

    The caller owns the transaction, so the notice is saved with the change itself."""
    if change not in CHANGES:
        raise ValueError("unknown memory change")
    if (
        type(revision) is not int
        or not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_uses'").fetchone()
    ):
        return False  # both callers hold an int (NOT NULL column); never a notice the read skips
    at = time.time() if now is None else now
    # Existence only: a chat in the Trash when the change finishes still counts, so its notice
    # appears once the chat is restored. A forgotten chat's rows are already deleted, and who may
    # be seen is decided again every time the Inbox is read.
    if not db.execute(
        "SELECT 1 FROM memory_uses WHERE memory_id=? AND recorded_at<=? "
        "AND (revision IS NULL OR revision<=?) LIMIT 1",
        (memory_id, at, revision),
    ).fetchone():
        return False
    return notices.insert(
        db,
        identity=_identity(request_id),
        kind=notices.MEMORY_RECALL,
        subject=request_id,
        title=STORED_TITLE,
        created_at=at,
        facts={"memoryId": memory_id, "change": change, "revision": revision},
    )


def _title(change: str, answers: int, chats: int) -> str:
    noun = "answer" if answers == 1 else "answers"
    verb = {"corrected": "corrected", "forgotten": "forgot"}.get(change, "changed")
    base = f"A memory you {verb} was used in {answers} {noun}"
    if not chats:
        return base
    return f"{base} in {chats} {'chat' if chats == 1 else 'chats'}"


def needs_you(store) -> list[dict]:
    """Open notices that still reach a visible answer, for the Inbox (newest first)."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM notices WHERE kind=? AND dismissed_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (notices.MEMORY_RECALL, MAX_OPEN),
        ).fetchall()
        items = []
        for row in rows:
            facts = notices.facts(row)
            memory_id, revision = facts.get("memoryId"), facts.get("revision")
            if not isinstance(memory_id, str) or type(revision) is not int:
                continue
            answers, chats = _reach(db, memory_id, revision, row["created_at"])
            if not answers:
                continue
            recorded = facts.get("change")
            change = recorded if isinstance(recorded, str) and recorded in CHANGES else "changed"
            sessions = [
                found[0]
                for found in db.execute(
                    "SELECT u.session_id FROM memory_uses u JOIN sessions s ON s.id=u.session_id "
                    "WHERE u.memory_id=? AND u.recorded_at<=? AND (u.revision IS NULL "
                    f"OR u.revision<=?) AND {_VISIBLE} "
                    "GROUP BY u.session_id ORDER BY MAX(u.recorded_at) DESC LIMIT ?",
                    (memory_id, row["created_at"], revision, MAX_CHATS),
                )
            ]
            items.append(
                {
                    "kind": "memory-recall",
                    "id": row["id"],
                    "title": _title(change, answers, chats),
                    "since": row["created_at"],
                    "memoryId": memory_id,
                    "change": change,
                    "answers": answers,
                    "chats": chats,
                    "sessionIds": sessions,
                    "source": {
                        "kind": "memory-recall",
                        "noticeId": row["id"],
                        "memoryId": memory_id,
                    },
                }
            )
    return items


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    """Hide a notice; the uses stay listed at `GET /memory/{id}/uses`."""
    if not re.fullmatch(NOTICE_ID, identity or ""):
        raise RecallNoticeError("not_found", "Notice not found.", 404)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT dismissed_at FROM notices WHERE id=? AND kind=?",
            (identity, notices.MEMORY_RECALL),
        ).fetchone()
        if row is None:
            raise RecallNoticeError("not_found", "Notice not found.", 404)
        changed = row["dismissed_at"] is None
        if changed:
            db.execute(
                "UPDATE notices SET dismissed_at=? WHERE id=?",
                (time.time() if now is None else now, identity),
            )
        db.commit()
    return {"id": identity, "dismissed": True, "changed": changed}
