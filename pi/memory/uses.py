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

import time

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
