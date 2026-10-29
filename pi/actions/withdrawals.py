"""Take back the ToolGate approval requests of a chat the owner forgot.

A parked turn waits on one approval request in ToolGate, and its Inbox card quotes the action
(the tool and its arguments, from the chat). Forgetting the chat runs with Pi stopped, so it only
queues each such request here (`queue`); once Pi runs again, `drain` asks ToolGate to withdraw it
(`ToolGateClient.withdraw`), acting as the same agent the asking invoke did. A request ToolGate no
longer knows, or one the owner already answered, is gone and needs nothing. A failure never undoes
the forgetting: it waits and is tried again later; Pi's key being refused is held as `blocked`,
reported in Pi's health and still tried again (hourly at most) until repaired. The queue holds
ids, never text.
"""

from __future__ import annotations

import time

from pi.actions.toolgate import ToolGateUnavailable

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_withdrawals (
 request_id TEXT PRIMARY KEY, agent_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('pending','withdrawn','gone','blocked')),
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS approval_withdrawals_due ON approval_withdrawals(state, next_at);
"""
# Pi's key itself was refused: retrying cannot help until the owner repairs it.
BLOCKED = {"withdraw_401", "withdraw_403"}


def initialize(db):
    db.executescript(SCHEMA)


def waiting(db, session_ids) -> list[tuple[str, str | None]]:
    """(request id, agent) of every parked turn in these chats still waiting on the owner."""
    if not session_ids:
        return []
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # A database from before parked turns or frozen settings existed has nothing, or no agent.
    if "turns" not in tables or "approval_request_id" not in {
        row[1] for row in db.execute("PRAGMA table_info(turns)")
    }:
        return []
    settings = "turn_settings" in tables
    marks = ",".join("?" for _ in session_ids)
    rows = db.execute(
        "SELECT t.approval_request_id, "
        + (
            "json_extract(s.snapshot,'$.agentId') FROM turns t LEFT JOIN turn_settings s ON s.turn_id=t.id "
            if settings
            else "NULL FROM turns t "
        )
        + "WHERE t.status='awaiting_approval' AND t.approval_request_id IS NOT NULL "
        f"AND t.session_id IN ({marks}) ORDER BY t.id",
        session_ids,
    ).fetchall()
    return [(row[0], row[1] or "companion") for row in rows]


def queue(db, session_ids, now=None) -> int:
    """Inside the forgetting transaction: queue each waiting request once."""
    found = waiting(db, session_ids)
    when = time.time() if now is None else now
    db.executemany(
        "INSERT OR IGNORE INTO approval_withdrawals(request_id,agent_id,state,created_at) "
        "VALUES (?,?,'pending',?)",
        [(request_id, agent, when) for request_id, agent in found],
    )
    return len(found)


def drain(store, client, now=None, limit=10) -> int:
    """Withdraw up to `limit` queued requests; how many are settled now."""
    if client is None or not getattr(client, "execution_key", "x"):
        return 0
    now = time.time() if now is None else now
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM approval_withdrawals WHERE state IN ('pending','blocked') AND next_at<=? "
            "ORDER BY next_at, request_id LIMIT ?",
            (now, limit),
        ).fetchall()
    settled = 0
    for row in rows:
        try:
            state, error = client.withdraw(row["request_id"], row["agent_id"]), ""
        except ToolGateUnavailable as exc:
            state = "blocked" if exc.reason in BLOCKED else "pending"
            error = (
                "ToolGate refused Pi's key; Pi tries again once it is repaired."
                if state == "blocked"
                else "ToolGate did not answer; trying again later."
            )
        with store._connect() as db:
            db.execute(
                "UPDATE approval_withdrawals SET state=?, attempts=attempts+1, next_at=?, error=? "
                "WHERE request_id=?",
                (
                    state,
                    now + min(3600, 30 * 2 ** min(row["attempts"], 7)),
                    error,
                    row["request_id"],
                ),
            )
        settled += state in ("withdrawn", "gone")
    return settled
