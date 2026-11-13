"""AI jobs Conker kept from a hidden chat: out of every list and not running while it is hidden.

A job Conker made on its own from a chat (keep_results kind `ai_job`) is hidden while that chat
is forgotten or in the Trash (pi.conversation.visibility), and a hidden job does not run either:
its scheduled slots pass without a run (they do not pile up for later), a webhook, watch or
"Run now" start is refused (`job_source_hidden`), and a run admitted just before the chat was
hidden is held back before its turn starts. Restoring the chat lets it run again; once the owner
adopts the job (`createdBy` owner) it is his and runs whatever happens to the chat. A forgotten
chat also turns the job off for good (agent_jobs.turn_off); this module changes no job.
"""

from __future__ import annotations

from pi.conversation import visibility

ADOPTED = "json_extract(definition,'$.createdBy')='owner'"  # his job, Conker kept it or not
# True for a scheduled_jobs row Conker kept from a hidden chat and the owner has not adopted.
PAUSED = (
    "(scheduled_jobs.id IN (SELECT target_id FROM keep_results WHERE kind='ai_job' AND "
    f"{visibility.hidden_sql('session_id')}) AND "
    "json_extract(scheduled_jobs.definition,'$.createdBy') IS NOT 'owner')"
)
REFUSAL = (
    "This job came from a chat in the Trash or forgotten, so it does not run; restoring that "
    "chat lets it run again."
)


def paused(db, job_id: str) -> bool:
    """True while this job may not start a run (see the module docstring)."""
    query = f"SELECT 1 FROM scheduled_jobs WHERE id=? AND {PAUSED}"
    return db.execute(query, (job_id,)).fetchone() is not None
