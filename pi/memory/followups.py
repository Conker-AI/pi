"""Forget in MemoryGate the memories Conker kept on its own from a chat the owner forgot.

Forgetting the chat queues them (`pi.agents.keep`); Pi does the forgetting here once it runs
again, through the same owner-reviewed path as forgetting one memory by hand (`pi.memory.forget`),
at the memory's current revision. A memory already gone counts as done. What cannot go now (memory
not connected, work running, another forget pending, MemoryGate down) waits and is tried again
later, never dropped; a refusal a retry alone cannot fix (a credential or receipt problem) is held as
`blocked` with its reason, reported in Pi's health and still tried again, hourly at most.
"""

from __future__ import annotations

import time

import httpx

from pi.memory import forget
from pi.memory.corrections import CorrectionError

RETRY = ("forget_pending", "work_active", "forget_in_progress", "outcome_unknown", "unavailable")


def _set(store, request_id, **fields):
    columns = ",".join(f"{name}=?" for name in fields)
    with store._connect() as db:
        db.execute(
            f"UPDATE keep_results SET {columns} WHERE request_id=?", (*fields.values(), request_id)
        )


def _later(store, row, error, now, undo="pending"):
    _set(
        store,
        row["request_id"],
        undo=undo,
        attempts=row["attempts"] + 1,
        next_at=now + min(3600, 30 * 2 ** min(row["attempts"], 7)),
        error=error,
    )


def drain(store, client, now=None, limit=5) -> int:
    """Forget up to `limit` queued memories; how many are settled now."""
    if client is None:
        return 0
    now = time.time() if now is None else now
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM keep_results WHERE kind='memory' AND undo IN ('pending','blocked') "
            "AND next_at<=? "
            "ORDER BY next_at, request_id LIMIT ?",
            (now, limit),
        ).fetchall()
    settled = 0
    for row in rows:
        revision = row["revision"]
        if revision is None:
            try:
                revision = client.memory(row["target_id"])["revision"]
            except CorrectionError as exc:
                if exc.status == 404:
                    _set(store, row["request_id"], undo="gone", error="")
                    settled += 1
                elif exc.status in (401, 403):
                    _later(store, row, "Memory refused Pi's key.", now, undo="blocked")
                else:
                    _later(store, row, f"Memory answered {exc.status}; trying again later.", now)
                continue
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                _later(store, row, "Memory did not answer; trying again later.", now)
                continue
            # Kept, so a retry after an unknown outcome repeats exactly this request.
            _set(store, row["request_id"], revision=revision)
        body = forget.Forget(
            request_id=f"keepforget-{row['request_id'][5:]}-r{revision}",
            memory_id=row["target_id"],
            expected_revision=revision,
        )
        try:
            forget.forget(store, client, body)
        except forget.ForgetError as exc:
            if exc.code == "not_found":
                _set(store, row["request_id"], undo="gone", error="")
                settled += 1
            elif exc.code == "revision_conflict":
                # It changed since: look at its revision again next time.
                _set(store, row["request_id"], revision=None)
                _later(store, row, "The memory changed; trying again.", now)
            elif exc.code in RETRY:
                _later(store, row, exc.message, now)
            else:
                _later(store, row, exc.message[:300], now, undo="blocked")
            continue
        _set(store, row["request_id"], undo="done", error="")
        settled += 1
    return settled
