"""What starts an AI job besides the clock: something new from a tool.

A watched job names a get tool, its inputs and how often to look. Every few minutes Pi calls the
tool through ToolGate exactly as a chat would, and compares a fingerprint of its answer with the
last one. Only a change starts the job; the agent then reads what is new with its own tools. Pi
keeps the fingerprint and when it last looked, never the answer itself.

A tool that needs the owner's OK each time cannot be watched silently: the first such answer
stops the watch with a plain reason instead of asking the owner every few minutes. Watches pause
during quiet hours; a change that arrives at night starts the job at the first look after.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from pi.schedule import jobs
from pi.schedule.job_models import Definition

WATCH_REQUEST = "watch_"
NEVER = 1e18
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_job_watch (
 job_id TEXT PRIMARY KEY REFERENCES scheduled_jobs(id),
 digest TEXT, checked_at REAL, next_check REAL NOT NULL, failure TEXT
);
"""
FAILURES = {
    "needs_approval": "This tool asks for your OK each time, so it can't start the job on its own.",
    "no_credential": "This agent has no tool access for scheduled work.",
    "failed": "The tool didn't answer; Conker tries again at the next check.",
}


def initialize(db):
    db.executescript(SCHEMA)


def fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def due(store, now: float) -> list[dict]:
    """Enabled AI jobs with a watch whose next look has come."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT j.id, j.definition, w.digest, w.checked_at FROM scheduled_jobs j "
            "LEFT JOIN agent_job_watch w ON w.job_id=j.id "
            "WHERE json_extract(j.definition,'$.watch') IS NOT NULL "
            "AND COALESCE(w.next_check, 0)<=? ORDER BY COALESCE(w.next_check, 0), j.id LIMIT 20",
            (now,),
        ).fetchall()
    found = []
    for row in rows:
        definition = Definition.model_validate_json(row["definition"])
        if definition.enabled and definition.watch is not None:
            found.append({**dict(row), "definition": definition})
    return found


def _save(store, job_id, *, digest, checked_at, next_check, failure=None):
    with store._connect() as db:
        db.execute(
            "INSERT INTO agent_job_watch VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "digest=excluded.digest, checked_at=excluded.checked_at, "
            "next_check=excluded.next_check, failure=excluded.failure",
            (job_id, digest, checked_at, next_check, failure),
        )


def check(store, client, row, now: float, *, quiet=False) -> str:
    """Look once. Returns what happened: baseline, unchanged, started, busy, paused or a failure."""
    watch = row["definition"].watch
    following = now + watch.everyMinutes * 60
    if quiet:
        _save(
            store,
            row["id"],
            digest=row["digest"],
            checked_at=row["checked_at"],
            next_check=following,
        )
        return "paused"
    if client is None:
        _save(
            store,
            row["id"],
            digest=row["digest"],
            checked_at=now,
            next_check=NEVER,
            failure="no_credential",
        )
        return "no_credential"
    try:
        result = client.invoke(watch.toolId, watch.args, action_id=WATCH_REQUEST + uuid.uuid4().hex)
    except Exception:
        # Refusals and transport errors are an answer for this look only; texts may hold inputs.
        _save(
            store,
            row["id"],
            digest=row["digest"],
            checked_at=now,
            next_check=following,
            failure="failed",
        )
        return "failed"
    if getattr(result, "request_id", None) is not None and not hasattr(result, "ok"):
        _save(
            store,
            row["id"],
            digest=row["digest"],
            checked_at=now,
            next_check=NEVER,
            failure="needs_approval",
        )
        return "needs_approval"
    if not getattr(result, "ok", False):
        _save(
            store,
            row["id"],
            digest=row["digest"],
            checked_at=now,
            next_check=following,
            failure="failed",
        )
        return "failed"
    digest = fingerprint(result.result)
    if row["digest"] is None:
        _save(store, row["id"], digest=digest, checked_at=now, next_check=following)
        return "baseline"
    if digest == row["digest"]:
        _save(store, row["id"], digest=digest, checked_at=now, next_check=following)
        return "unchanged"
    # One run per change: the same change seen again (after a crash) replays the same request.
    request_id = f"{WATCH_REQUEST}{digest[:32]}_{int(row['checked_at'] or 0)}"
    try:
        jobs.run_now(store, row["id"], request_id, now=now)
    except jobs.JobError:
        # A run is still going: keep the old fingerprint so the next look starts the job.
        _save(store, row["id"], digest=row["digest"], checked_at=now, next_check=following)
        return "busy"
    _save(store, row["id"], digest=digest, checked_at=now, next_check=following)
    return "started"


def started_by_watch(request_id: str | None) -> bool:
    return bool(request_id) and request_id.startswith(WATCH_REQUEST)


def reset(store, job_id):
    """A changed watch starts over: a new first look, no old failure."""
    with store._connect() as db:
        db.execute("DELETE FROM agent_job_watch WHERE job_id=?", (job_id,))
