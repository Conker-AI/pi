"""AI job notes: the Inbox lines a job leaves, and their forgetting.

Split out of `pi.schedule.agent_jobs`; a note is a `notices` row of kind `agent-job`.
"""

import re
import time

from pi.conversation import visibility
from pi.owner import notices
from pi.schedule.agent_jobs import NOTE_ID, AgentJobError


def redact(db, session_ids: list[str]) -> None:
    """Forget with the chat, inside the caller's forgetting transaction: a note quotes its reply.

    The job itself stays; its next run starts a new chat. Its notes are notices, cleared through
    `notices.redact`. A database a Pi from before migration 0006 wrote to again (a code
    rollback without the snapshot) can hold the old agent_job_notes table: cleared here too.
    """
    if not session_ids:
        return
    notices.redact(db, session_ids)
    values = [(value,) for value in session_ids]
    for table in ("agent_job_notes", "agent_job_sessions"):
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            db.executemany(f"DELETE FROM {table} WHERE session_id=?", values)


def notes(store, limit=50):
    """Open notes, newest first; a note from a forgotten or trashed chat is not listed."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT n.* FROM notices n JOIN sessions s ON s.id=n.session_id "
            "WHERE n.kind=? AND n.dismissed_at IS NULL "
            f"AND NOT {visibility.hidden_sql('n.session_id')} "
            "ORDER BY n.created_at DESC LIMIT ?",
            (notices.AGENT_JOB, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "jobId": notices.facts(row).get("jobId"),
            "sessionId": row["session_id"],
            "messageId": notices.facts(row).get("messageId"),
            "title": row["title"],
            "detail": row["detail"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def dismiss(store, identity, now=None):
    if not re.fullmatch(NOTE_ID, identity or ""):
        raise AgentJobError("not_found", "Note not found.", 404)
    with store._connect() as db:
        changed = db.execute(
            "UPDATE notices SET dismissed_at=? WHERE id=? AND kind=? AND dismissed_at IS NULL",
            (time.time() if now is None else now, identity, notices.AGENT_JOB),
        ).rowcount
        exists = db.execute(
            "SELECT 1 FROM notices WHERE id=? AND kind=?", (identity, notices.AGENT_JOB)
        ).fetchone()
    if not exists:
        raise AgentJobError("not_found", "Note not found.", 404)
    return {"id": identity, "dismissed": True, "changed": bool(changed)}


def needs_you(store):
    """Open notes for the Inbox: one line each, linking to the job's chat."""
    return [
        {
            "kind": "agent-job",
            "id": note["id"],
            "title": note["title"],
            "since": note["createdAt"],
            "source": {"kind": "agent-job", "noteId": note["id"], "sessionId": note["sessionId"]},
        }
        for note in notes(store)
    ]
