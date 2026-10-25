"""Change requests: what Conker asks to create, waiting for the owner's verification.

Conker can prepare anything the owner could make by hand (a cron job, a tool draft, a memory) but
never make it real on its own. It files a request with the exact thing to create; the Inbox shows
it, and only the owner's approval creates it, through the same paths the owner's own screens use.
A request is never edited after it is filed: what the owner approves is exactly what Conker asked.

- `ai_job`: approving creates the AI job in Pi (`agent_jobs.create`), so the owner's password
  verification on the approval is the only way it starts to run.
- `tool_draft`: Pi holds no ToolGate authority. The owner's browser saves the document as a draft
  in ToolGate's editor and reports the draft's id here; the owner then publishes it there.
- `memory`: approving writes the stated text to memory (when MemoryGate offers that route).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Literal

from pydantic import Field

from pi.core.errors import ServiceError
from pi.core.models import Strict

KINDS = ("ai_job", "tool_draft", "memory")
REQUEST_ID = r"^creq_[a-f0-9]{32}$"
MAX_PAYLOAD = 200_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS change_requests (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('ai_job','tool_draft','memory')),
 title TEXT NOT NULL, summary TEXT NOT NULL, payload TEXT NOT NULL,
 session_id TEXT REFERENCES sessions(id), turn_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('open','approved','declined','failed')),
 result TEXT, created_at REAL NOT NULL, decided_at REAL
);
CREATE INDEX IF NOT EXISTS change_requests_open ON change_requests(state, created_at);
"""


class ChangeRequestError(ServiceError):
    pass


class DraftSaved(Strict):
    """The owner's browser saved the requested document in ToolGate's editor."""

    draftId: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


class Approve(Strict):
    """The approval body: empty, or the saved draft's id for a tool draft."""

    draftId: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


class View(Strict):
    id: str = Field(pattern=REQUEST_ID)
    kind: Literal["ai_job", "tool_draft", "memory"]
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(max_length=600)
    payload: dict
    sessionId: str | None = None
    state: Literal["open", "approved", "declined", "failed"]
    result: dict | None = None
    createdAt: float = Field(ge=0)
    decidedAt: float | None = None


def initialize(db):
    db.executescript(SCHEMA)


def _view(row) -> dict:
    return View(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        summary=row["summary"],
        payload=json.loads(row["payload"]),
        sessionId=row["session_id"],
        state=row["state"],
        result=json.loads(row["result"]) if row["result"] else None,
        createdAt=row["created_at"],
        decidedAt=row["decided_at"],
    ).model_dump()


def file(store, kind, title, summary, payload, *, session_id=None, turn_id=None, now=None) -> dict:
    """Record one request exactly as Conker asked; nothing is created yet."""
    if kind not in KINDS:
        raise ValueError("Unknown change request kind.")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded) > MAX_PAYLOAD:
        raise ChangeRequestError("too_large", "This request is too large to review.", 422)
    identity = "creq_" + uuid.uuid4().hex
    with store._connect() as db:
        db.execute(
            "INSERT INTO change_requests VALUES (?,?,?,?,?,?,?,'open',NULL,?,NULL)",
            (
                identity,
                kind,
                title[:160] or "Conker's request",
                summary[:600],
                encoded,
                session_id,
                turn_id,
                time.time() if now is None else now,
            ),
        )
        return _view(db.execute("SELECT * FROM change_requests WHERE id=?", (identity,)).fetchone())


def get(store, identity) -> dict:
    if not re.fullmatch(REQUEST_ID, identity or ""):
        raise ChangeRequestError("not_found", "Request not found.", 404)
    with store._connect() as db:
        row = db.execute("SELECT * FROM change_requests WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ChangeRequestError("not_found", "Request not found.", 404)
    return _view(row)


def listing(store, state="open", limit=50) -> list[dict]:
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM change_requests WHERE state=? ORDER BY created_at DESC LIMIT ?",
            (state, limit),
        ).fetchall()
    return [_view(row) for row in rows]


def _close(store, identity, state, result, now):
    with store._connect() as db:
        changed = db.execute(
            "UPDATE change_requests SET state=?, result=?, decided_at=? WHERE id=? AND state='open'",
            (
                state,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                now,
                identity,
            ),
        ).rowcount
    if not changed:
        raise ChangeRequestError("decided", "This request was already decided.", 409)
    return get(store, identity)


def approve(store, identity, *, draft=None, memory=None, now=None) -> dict:
    """Make the request real, exactly as filed. Only the owner's approval route calls this."""
    now = time.time() if now is None else now
    request = get(store, identity)
    if request["state"] != "open":
        raise ChangeRequestError("decided", "This request was already decided.", 409)
    if request["kind"] == "ai_job":
        from pi.schedule import agent_jobs
        from pi.schedule.job_models import AgentJobCreate

        try:
            body = AgentJobCreate.model_validate(request["payload"])
        except ValueError:
            return _close(
                store, identity, "failed", {"reason": "The requested job is no longer valid."}, now
            )
        job = agent_jobs.create(store, body, now=now)
        return _close(store, identity, "approved", {"jobId": job["id"]}, now)
    if request["kind"] == "tool_draft":
        if draft is None:
            raise ChangeRequestError(
                "draft_required", "Save the draft in Tools first; then approve with its id.", 422
            )
        saved = DraftSaved.model_validate(
            draft.model_dump() if hasattr(draft, "model_dump") else draft
        )
        return _close(store, identity, "approved", {"draftId": saved.draftId}, now)
    if memory is None:
        raise ChangeRequestError(
            "memory_unavailable", "Saving memories from a request is not available yet.", 503
        )
    try:
        result = memory(request["payload"]["text"])
    except Exception:
        return _close(
            store, identity, "failed", {"reason": "Memory didn't save; nothing was changed."}, now
        )
    return _close(store, identity, "approved", result, now)


def decline(store, identity, now=None) -> dict:
    return _close(store, identity, "declined", None, time.time() if now is None else now)


def redact(db, session_ids: list[str]) -> None:
    """Forget with the chat: a request quotes what was said there."""
    if (
        not session_ids
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='change_requests'"
        ).fetchone()
    ):
        return
    db.executemany(
        "DELETE FROM change_requests WHERE session_id=?", [(value,) for value in session_ids]
    )


def needs_you(store) -> list[dict]:
    return [
        {
            "kind": "change-request",
            "id": item["id"],
            "title": item["title"],
            "since": item["createdAt"],
            "source": {
                "kind": "change-request",
                "requestId": item["id"],
                "requestKind": item["kind"],
            },
        }
        for item in listing(store)
    ]
