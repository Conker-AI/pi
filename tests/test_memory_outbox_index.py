"""The once-a-second outbox poll reads due work from a partial index.

`memory_outbox` keeps one row per user message forever (sent rows are receipts), so
`MemoryService.drain_once` would otherwise scan the whole table on every idle tick.
"""

import time

from pi.conversation.store import Store

DUE_QUERY = (
    "SELECT * FROM memory_outbox WHERE state='pending' AND next_at<=?"
    " ORDER BY operation='delete' DESC,next_at,message_id LIMIT ?"
)


def test_due_memory_work_is_found_from_an_index(tmp_path):
    store = Store(tmp_path / "pi.db")
    with store._connect() as db:
        plan = db.execute("EXPLAIN QUERY PLAN " + DUE_QUERY, (time.time(), 20)).fetchall()
    assert "memory_outbox_due" in " ".join(row[-1] for row in plan)


def test_the_index_holds_only_pending_rows(tmp_path):
    store = Store(tmp_path / "pi.db")
    lookup = "SELECT sql FROM sqlite_master WHERE name='memory_outbox_due'"
    with store._connect() as db:
        sql = db.execute(lookup).fetchone()[0]
    assert "WHERE state='pending'" in sql
