"""Webhooks: anything that can call a web address can start an AI job.

The owner makes a webhook for one AI job and gets its secret once; Pi keeps only a SHA-256
fingerprint. A caller (an MCP listener, n8n, a phone shortcut, a script) sends a small JSON body
to the gateway's `/hooks/{job_id}` with the secret in the `X-Conker-Hook-Token` header, never in
the address, because request paths are written to the server's access log.

Each call admits at most one run, at most one every 30 seconds per job, and never while the job
is still running. What the caller sent waits for that run and is handed to the agent as
information from outside Conker, never as instructions; it is deleted when the run takes it.
Tools still ask exactly as in chat, so a webhook can never change anything without the owner.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid

from pi.core.errors import ServiceError
from pi.schedule import jobs
from pi.schedule.job_models import Definition

HOOK_REQUEST = "hook_"
TOKEN = re.compile(r"^hook_[A-Za-z0-9_-]{43}$")
MIN_GAP_SECONDS = 30
MAX_PAYLOAD = 8000
STALE_EVENT_SECONDS = 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_job_hooks (
 job_id TEXT PRIMARY KEY REFERENCES scheduled_jobs(id),
 token_hash TEXT NOT NULL, created_at REAL NOT NULL, last_at REAL
);
CREATE TABLE IF NOT EXISTS agent_job_events (
 run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
);
"""


class HookError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _agent_job(store, job_id) -> Definition:
    definition = Definition.model_validate(jobs.get_job(store, job_id)["definition"])
    if definition.target.kind != "agent":
        raise HookError("not_agent_job", "Only AI jobs can have a webhook.", 409)
    return definition


def issue(store, job_id, now=None) -> dict:
    """A new secret for this job's webhook; any earlier one stops working. Shown only now."""
    _agent_job(store, job_id)
    token = HOOK_REQUEST + secrets.token_urlsafe(32)
    with store._connect() as db:
        db.execute(
            "INSERT INTO agent_job_hooks VALUES (?,?,?,NULL) ON CONFLICT(job_id) DO UPDATE SET "
            "token_hash=excluded.token_hash, created_at=excluded.created_at, last_at=NULL",
            (job_id, _hash(token), time.time() if now is None else now),
        )
    return {
        "jobId": job_id,
        "token": token,
        "header": "X-Conker-Hook-Token",
        "path": f"/hooks/{job_id}",
    }


def revoke(store, job_id) -> dict:
    with store._connect() as db:
        db.execute("DELETE FROM agent_job_hooks WHERE job_id=?", (job_id,))
    return {"jobId": job_id, "revoked": True}


def configured(store, job_id) -> bool:
    with store._connect() as db:
        return bool(
            db.execute("SELECT 1 FROM agent_job_hooks WHERE job_id=?", (job_id,)).fetchone()
        )


def _payload(body) -> str:
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= MAX_PAYLOAD else text[: MAX_PAYLOAD - 1] + "…"


def receive(store, job_id, token, body, now=None) -> dict:
    """Start the job once for this call, or say plainly why not. Never raises on a busy job."""
    now = time.time() if now is None else now
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise HookError("hook_refused", "Webhook refused.", 403)
    with store._connect() as db:
        db.execute("DELETE FROM agent_job_events WHERE created_at<?", (now - STALE_EVENT_SECONDS,))
        row = db.execute(
            "SELECT token_hash, last_at FROM agent_job_hooks WHERE job_id=?", (job_id,)
        ).fetchone()
    # One answer for every refusal, so a caller cannot probe which jobs exist.
    if row is None or not secrets.compare_digest(row["token_hash"], _hash(token)):
        raise HookError("hook_refused", "Webhook refused.", 403)
    definition = _agent_job(store, job_id)
    if not definition.enabled:
        return {"started": False, "reason": "paused"}
    if row["last_at"] is not None and now - row["last_at"] < MIN_GAP_SECONDS:
        raise HookError("hook_too_soon", "Wait 30 seconds between webhook calls.", 429)
    request_id = HOOK_REQUEST + uuid.uuid4().hex
    try:
        run = jobs.run_now(store, job_id, request_id, now=now)
    except jobs.JobError:
        return {"started": False, "reason": "busy"}
    with store._connect() as db:
        db.execute("UPDATE agent_job_hooks SET last_at=? WHERE job_id=?", (now, job_id))
        db.execute(
            "INSERT INTO agent_job_events VALUES (?,?,?,?)",
            (run["id"], job_id, _payload(body), now),
        )
    return {"started": True, "runId": run["id"]}


def started_by_hook(request_id: str | None) -> bool:
    return bool(request_id) and request_id.startswith(HOOK_REQUEST)


def take(store, run_id) -> str | None:
    """The caller's payload for this run, once; it is deleted as it is read."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM agent_job_events WHERE run_id=?", (run_id,)
        ).fetchone()
        db.execute("DELETE FROM agent_job_events WHERE run_id=?", (run_id,))
        db.commit()
    return row["payload"] if row else None


def redact(db, session_ids: list[str]) -> None:
    """Forgetting a job's chat also drops payloads still waiting for its runs."""
    if (
        not session_ids
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_job_events'"
        ).fetchone()
    ):
        return
    db.executemany(
        "DELETE FROM agent_job_events WHERE job_id IN "
        "(SELECT job_id FROM agent_job_sessions WHERE session_id=?)",
        [(value,) for value in session_ids],
    )
