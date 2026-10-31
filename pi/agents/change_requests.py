"""Change requests: what Conker asks to create, waiting for the owner's verification.

Conker can prepare anything the owner could make by hand (a cron job, a tool draft, a memory). It
files a request with the exact thing to create; the Inbox shows it, and the owner's approval
creates it, through the same paths the owner's own screens use. A request is never edited after it
is filed: what the owner approves is exactly what Conker asked. The one exception is a tool
request, whose draft Pi fills in after filing (`pi.agents.tool_drafts`).

Only where the owner's own access settings allow it (`pi.turns.access`) does Pi approve a request
at once in Conker's name (Keep, `by="conker"`). Remembering is allowed by default; scheduling
waits for him by default; a tool draft always waits. Every Keep result is kept apart, linked to
its chat (`pi.agents.keep`), so forgetting the chat can undo it.

- `ai_job`: approving creates the AI job in Pi (`agent_jobs.create`). Approved by the owner
  (password verified) it is his; made at once it is marked `createdBy: "conker"` and held
  tighter (see `pi.schedule.agent_jobs`). A job asked for in a private chat gets a private chat.
- `tool_draft`: Pi holds no ToolGate authority. Once the draft is ready, the owner's browser saves
  the document as a draft in ToolGate's editor (password verified there) and reports the draft's
  id here (`saved`), which creates nothing; the owner then publishes it there.
- `memory`: approving stores the exact text through MemoryGate's statement route, keyed by the
  request id, so a retry after a lost answer never stores it twice. Approved by the owner it is
  his statement; made at once it is stored as Conker's (`origin: "conker"`) under its own key, so
  his later approval of the same request never collides with it.

Conker keeps at most `PER_CHAT` requests open per chat and `OPEN_LIMIT` in all, so outside
content cannot flood the Inbox; the owner's own requests (`/tool-drafts`) are not limited.
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
PER_CHAT = 3
OPEN_LIMIT = 20
# MemoryGate keys a Keep memory apart from the owner's approval of the same request.
CONKER_KEY = "-conker"
# Terminal control characters (C0 except tab and newline, DEL, C1): titles and summaries come from
# the model or outside content and are printed by the command line.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
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


class Full(ValueError):
    """Too many of Conker's requests already wait for the owner; a plain answer for the model."""


class DraftSaved(Strict):
    """The owner's browser saved the requested document in ToolGate's editor."""

    draftId: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


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


def encode(payload) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded) > MAX_PAYLOAD:
        raise ChangeRequestError("too_large", "This request is too large to review.", 422)
    return encoded


def title_text(title: str) -> str:
    """A card title on one line, without control characters."""
    return " ".join(CONTROL.sub("", title).split())[:160] or "Conker's request"


def summary_text(summary: str) -> str:
    """A card summary without control characters; its lines stay."""
    return CONTROL.sub("", summary)[:600]


def file(store, kind, title, summary, payload, *, session_id=None, turn_id=None, now=None) -> dict:
    """Record one request exactly as Conker asked; nothing is created yet. Full when too many of
    its requests already wait for the owner."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            session_id is not None
            and db.execute(
                "SELECT COUNT(*) FROM change_requests WHERE state='open' AND session_id=?",
                (session_id,),
            ).fetchone()[0]
            >= PER_CHAT
        ):
            raise Full(
                f"{PER_CHAT} of Conker's requests from this chat already wait in the owner's "
                "Inbox. Nothing new was filed: tell the owner, and ask again once they decide."
            )
        if db.execute("SELECT COUNT(*) FROM change_requests WHERE state='open'").fetchone()[0] >= (
            OPEN_LIMIT
        ):
            raise Full(
                f"{OPEN_LIMIT} requests already wait in the owner's Inbox. Nothing new was filed: "
                "tell the owner, and ask again once they have decided some."
            )
        view = file_in(db, kind, title, summary, payload, session_id, turn_id, now)
        db.commit()
        return view


def file_in(db, kind, title, summary, payload, session_id=None, turn_id=None, now=None) -> dict:
    """`file` inside the caller's transaction."""
    if kind not in KINDS:
        raise ValueError("Unknown change request kind.")
    encoded = encode(payload)
    identity = "creq_" + uuid.uuid4().hex
    db.execute(
        "INSERT INTO change_requests VALUES (?,?,?,?,?,?,?,'open',NULL,?,NULL)",
        (
            identity,
            kind,
            title_text(title),
            summary_text(summary),
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


def _privacy(store, session_id) -> dict | None:
    """The flags of the chat a request came from, for the job made from it; unknown is private."""
    if session_id is None:
        return None
    from pi.conversation import settings as session_settings

    with store._connect() as db:
        db.execute("BEGIN")
        privacy = session_settings.source_privacy(db, session_id)
    if privacy is None:
        return {"memoryDisabled": True, "harnessDisabled": True}
    flags = {key: True for key in ("memoryDisabled", "harnessDisabled") if privacy.get(key)}
    return flags or None


def approve(store, identity, *, draft=None, memory=None, by="owner", now=None) -> dict:
    """Make the request real, exactly as filed: by the owner's approval route, or by Conker
    (`by="conker"`) where his access settings let it go ahead without asking (Keep)."""
    if by not in ("owner", "conker"):
        raise ValueError("Approve as the owner or as Conker.")
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
        full = agent_jobs.room(store, by, now)
        if full:
            raise ChangeRequestError("job_limit", full, 409)
        job = agent_jobs.create(
            store, body, now=now, created_by=by, privacy=_privacy(store, request["sessionId"])
        )
        return _kept(
            store, request, by, _close(store, identity, "approved", {"jobId": job["id"]}, now)
        )
    if request["kind"] == "tool_draft":
        # A request filed before drafting moved to the background has no status: it is ready.
        if request["payload"].get("status", "ready") != "ready":
            raise ChangeRequestError("not_ready", "This tool is not drafted yet.", 409)
        if draft is None:
            raise ChangeRequestError(
                "draft_required", "Save the draft in Tools first; then report its id.", 422
            )
        saved = DraftSaved.model_validate(
            draft.model_dump() if hasattr(draft, "model_dump") else draft
        )
        return _close(store, identity, "approved", {"draftId": saved.draftId}, now)
    if memory is None:
        raise ChangeRequestError(
            "memory_unavailable", "Memory isn't connected, so nothing was saved.", 503
        )
    try:
        text = request["payload"]["text"]
        # The model wrote a Keep memory's words; only his approval makes them his statement.
        result = (
            memory(identity + CONKER_KEY, text, origin="conker")
            if by == "conker"
            else memory(identity, text)
        )
    except Exception:
        # The request stays open: retrying with the same id is safe on MemoryGate's side.
        raise ChangeRequestError(
            "memory_unavailable", "Memory didn't answer, so nothing was saved. Try again.", 503
        ) from None
    return _kept(store, request, by, _close(store, identity, "approved", result, now))


def _kept(store, request, by, decided) -> dict:
    """Link what Conker made on its own to its chat, outside this table, for forgetting."""
    if by == "conker" and request["sessionId"]:
        from pi.agents import keep

        keep.record(store, request, decided["result"] or {})
    return decided


def saved(store, identity, draft: DraftSaved, now=None) -> dict:
    """Record the Tools draft the owner saved from a tool request. Only tool requests: a job or
    a memory is made real by `approve` alone."""
    if get(store, identity)["kind"] != "tool_draft":
        raise ChangeRequestError("not_a_tool_draft", "Only a tool request is saved in Tools.", 409)
    return approve(store, identity, draft=draft, now=now)


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
