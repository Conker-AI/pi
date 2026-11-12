"""What Conker made on its own (Keep), linked to the chat it was made in.

A change request is deleted when its chat is forgotten (it quotes the chat), so Pi keeps a second
record of each Keep result that holds no text: the chat, the request id, and the memory, AI job,
agent or team made. Forgetting the chat (`pi.conversation.forgetting`) then undoes what it can:
each memory it kept is queued to be forgotten in MemoryGate once Pi runs again
(`pi.memory.followups`), each AI job it made is turned off and marked (never deleted; the owner can
turn it back on), and each agent or team it made is archived (the owner can restore it; its stored
configuration stays, like a job's instructions). A memory MemoryGate already held
("already_remembered") was not made by that chat and is left alone. What the owner approved
himself is his and stays.
"""

from __future__ import annotations

import time

from pi.core import rebuild
from pi.schedule import agent_jobs

TABLE = """
CREATE TABLE IF NOT EXISTS keep_results (
 request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 kind TEXT NOT NULL CHECK(kind IN ('memory','ai_job','agent','team')), target_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 undo TEXT CHECK(undo IN ('pending','done','gone','blocked')),
 revision INTEGER, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT ''
);
"""
SCHEMA = (
    TABLE
    + """CREATE INDEX IF NOT EXISTS keep_results_session ON keep_results(session_id);
CREATE INDEX IF NOT EXISTS keep_results_undo ON keep_results(undo, next_at);
"""
)
# The result field that names what each kind of request made.
TARGETS = {"ai_job": "jobId", "agent": "agentId", "team": "teamId"}


def initialize(db):
    # Agents and teams arrived 2026-11-12: an older table's CHECK refuses them until rebuilt.
    rebuild.widen(db, "keep_results", TABLE, "'team'")
    db.executescript(SCHEMA)


def record(db, request, result, now=None) -> None:
    """One Keep result, inside the caller's transaction: the AI job, agent or team made, or the
    memory MemoryGate stored new for it."""
    if request["kind"] in TARGETS:
        target = result.get(TARGETS[request["kind"]])
    elif request["kind"] == "memory" and result.get("status") == "remembered":
        target = result.get("memoryId")
    else:
        return
    if not isinstance(target, str) or not target or not request.get("sessionId"):
        return
    db.execute(
        "INSERT OR IGNORE INTO keep_results(request_id,session_id,kind,target_id,created_at) "
        "VALUES (?,?,?,?,?)",
        (
            request["id"],
            request["sessionId"],
            request["kind"],
            target,
            time.time() if now is None else now,
        ),
    )


def listing(store, session_id) -> list[dict]:
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM keep_results WHERE session_id=? ORDER BY created_at", (session_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def forget_chats(db, session_ids) -> dict:
    """Inside the forgetting transaction: queue each kept memory to be forgotten, turn off each AI
    job and archive each agent and team these chats made on their own. Counts, for the
    forgetting receipt."""
    if (
        not session_ids
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='keep_results'"
        ).fetchone()
    ):
        return {"memories": 0, "jobs": 0, "agents": 0, "teams": 0}
    marks = "(" + ",".join("?" for _ in session_ids) + ")"
    queued = db.execute(
        "UPDATE keep_results SET undo='pending', next_at=0 WHERE kind='memory' AND undo IS NULL "
        f"AND session_id IN {marks}",
        session_ids,
    ).rowcount
    jobs = [
        row[0]
        for row in db.execute(
            "SELECT target_id FROM keep_results WHERE kind='ai_job' AND undo IS NULL "
            f"AND session_id IN {marks}",
            session_ids,
        )
    ]
    agent_jobs.turn_off(db, jobs)
    db.execute(
        f"UPDATE keep_results SET undo='done' WHERE kind='ai_job' AND session_id IN {marks}",
        session_ids,
    )
    made = {"agents": 0, "teams": 0}
    for row in db.execute(
        "SELECT kind,target_id FROM keep_results WHERE kind IN ('agent','team') AND undo IS NULL "
        f"AND session_id IN {marks}",
        session_ids,
    ).fetchall():
        from pi.agents import agent_requests

        made[row["kind"] + "s"] += agent_requests.archive_in(db, row["kind"], row["target_id"])
    db.execute(
        "UPDATE keep_results SET undo='done' WHERE kind IN ('agent','team') "
        f"AND session_id IN {marks}",
        session_ids,
    )
    return {"memories": queued, "jobs": len(jobs), **made}
