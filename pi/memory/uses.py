"""Which turns used which memories (R-U3): ids only, kept when the recall packages are wiped.

A memory correction or forget wipes every saved recall package (`store.invalidate_recall`), so
"which answers used this memory" could not be answered afterwards. This table is written beside
the packages (the turn's own recall and the `conker.recall` tool) and holds no words: a turn, its
chat, a memory id, the revision the package carried when it had one, and which of the two
sources it came from. Forgetting a chat deletes that chat's rows; a chat in the Trash is hidden
at read time (`pi.conversation.visibility`).

Design: context/design/memory-recall-notice.md in the Conker workspace.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from pi.conversation import visibility
from pi.core.errors import ServiceError
from pi.core.models import Strict

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_uses (
 turn_id TEXT NOT NULL REFERENCES turns(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 memory_id TEXT NOT NULL, revision INTEGER,
 source TEXT NOT NULL CHECK(source IN ('context','recall')), recorded_at REAL NOT NULL,
 PRIMARY KEY (turn_id, memory_id, source)
);
CREATE INDEX IF NOT EXISTS memory_uses_memory ON memory_uses(memory_id, recorded_at);
CREATE INDEX IF NOT EXISTS memory_uses_session ON memory_uses(session_id);
"""
SOURCES = ("context", "recall")
MAX_ID = 200  # a longer id is not one of MemoryGate's
MEMORY_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"
CURSOR = re.compile(r"^([0-9]{1,16}(?:\.[0-9]{1,9})?)\|([A-Za-z0-9_-]{1,128})\|(context|recall)$")
BAD_CURSOR = "Use the nextAfter value the last page gave."


class UseError(ServiceError):
    pass


class MemoryUse(Strict):
    sessionId: str
    sessionTitle: str
    turnId: str
    source: Literal["context", "recall"]
    revision: int | None
    at: float


class UsePage(Strict):
    memoryId: str
    # When Pi began to note uses; a turn before it has no row, whatever it recalled.
    since: float | None
    total: int
    chats: int
    items: list[MemoryUse]
    nextAfter: str | None


def _memories(package) -> list[tuple[str, int | None]]:
    """(id, revision) of each memory in a recall package, once each, in order; no words."""
    items = package.get("memories") if isinstance(package, dict) else None
    seen: dict[str, int | None] = {}
    for item in items if isinstance(items, list) else []:
        identity = item.get("id") if isinstance(item, dict) else None
        if not isinstance(identity, str) or not 0 < len(identity) <= MAX_ID or identity in seen:
            continue
        revision = item.get("revision")
        seen[identity] = revision if type(revision) is int and 0 < revision < 2**31 else None
    return list(seen.items())


def record(db, turn_id: str, session_id: str, package, source: str) -> int:
    """Note the memories a turn's package carried; the caller owns the transaction."""
    if source not in SOURCES:
        raise ValueError("unknown memory use source")
    now = time.time()
    rows = [
        (turn_id, session_id, identity, revision, source, now)
        for identity, revision in _memories(package)
    ]
    db.executemany(
        "INSERT OR IGNORE INTO memory_uses(turn_id,session_id,memory_id,revision,source,"
        "recorded_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def redact(db, session_ids) -> None:
    """Forgetting: a chat's use rows go with it (the caller owns the transaction)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_uses'").fetchone():
        return  # a database from before this table; the Store creates it at start
    for session_id in session_ids:
        db.execute("DELETE FROM memory_uses WHERE session_id=?", (session_id,))


def listing(store, memory_id: str, limit: int, after: str | None) -> dict:
    """Turns in visible chats that used one memory, newest first (read only, ids and titles).

    A chat that is forgotten or in the Trash is left out, and returns when restored."""
    if not re.fullmatch(MEMORY_ID, memory_id) or len(memory_id) > MAX_ID:
        raise UseError("invalid_memory_id", "That is not a memory id.", 422)
    visible = f"s.status<>'forgotten' AND NOT {visibility.hidden_sql('u.session_id')}"
    where = f"u.memory_id=? AND {visible}"
    params: list = [memory_id]
    anchor = None
    if after is not None:
        found = CURSOR.fullmatch(after)
        if not found:
            raise UseError("invalid_cursor", BAD_CURSOR, 422)
        anchor = (float(found.group(1)), found.group(2), found.group(3))
    with store._connect() as db:
        base = "FROM memory_uses u JOIN sessions s ON s.id=u.session_id"
        total, chats = db.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT u.session_id) {base} WHERE {where}", params
        ).fetchone()
        since = db.execute("SELECT MIN(recorded_at) FROM memory_uses").fetchone()[0]
        page_where = where
        if anchor:
            page_where += " AND (u.recorded_at, u.turn_id, u.source) < (?,?,?)"
            params.extend(anchor)
        rows = db.execute(
            "SELECT u.session_id, s.title, u.turn_id, u.source, u.revision, u.recorded_at "
            f"{base} WHERE {page_where} "
            "ORDER BY u.recorded_at DESC, u.turn_id DESC, u.source DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    items = [
        {
            "sessionId": r[0],
            "sessionTitle": r[1] or "",
            "turnId": r[2],
            "source": r[3],
            "revision": r[4],
            "at": r[5],
        }
        for r in rows[:limit]
    ]
    last = rows[limit - 1] if len(rows) > limit else None
    return {
        "memoryId": memory_id,
        "since": since,
        "total": total,
        "chats": chats,
        "items": items,
        "nextAfter": f"{last[5]!r}|{last[2]}|{last[3]}" if last else None,
    }
