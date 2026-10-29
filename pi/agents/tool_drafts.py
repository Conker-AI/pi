"""Tool requests: filed at once, drafted in the background, ready or failed for the owner.

`conker.draft_tool` in chat and `POST /tool-drafts` (the Tools screen and the command line) file
the same `tool_draft` change request (`pi.agents.change_requests`) with the payload
`{"description", "status": "drafting"}` and answer at once. Pi's drafting worker
(`pi.turns.tool_drafting`) drafts one request at a time (`build`): the drafting model sees only the
capabilities that match the description (`pi.agents.draft_capabilities`), ToolGate's read-only
document check reads the result, and the model repairs it up to twice from what the check found.
The payload then becomes `{"description", "status": "ready", "document", "issues", "steps",
"checked"}` (with a `note` when ToolGate could not check it) or `{"description", "status":
"failed", "error"}`. The request stays open either way; a request filed before this has no status
and counts as ready. The owner can draft it again (`retry`, optionally with a new description) and
tells Pi when the tool is live (`published`), which Conker then says in the chat that asked.
"""

from __future__ import annotations

import hashlib
import json
import time

from pydantic import Field, field_validator

from pi.agents import change_requests, draft_capabilities, workflow_drafts
from pi.core.errors import ServiceError
from pi.core.models import Strict

DRAFTING, READY, FAILED = "drafting", "ready", "failed"
RESTARTED = "Pi restarted while drafting. Try again."
NOT_CHECKED = "ToolGate could not check this draft. Tools checks it again when you open it."
REPAIRS = 2
REQUEST_ID = r"^[A-Za-z0-9_.:-]{16,128}$"
TOOL_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_draft_submissions (
 request_id TEXT PRIMARY KEY,
 change_request_id TEXT NOT NULL,
 description_sha256 TEXT NOT NULL,
 created_at REAL NOT NULL
);
"""


class ToolDraftError(ServiceError):
    pass


def _words(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("Describe the tool in words.")
    return value.strip() if value is not None else None


class NewDraft(Strict):
    """A tool request made without a chat; the same request_id gives back the same request."""

    description: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(pattern=REQUEST_ID)

    _description = field_validator("description")(_words)


class Retry(Strict):
    """Draft it again, from a new description when one is given."""

    description: str | None = Field(default=None, min_length=1, max_length=4000)

    _description = field_validator("description")(_words)


class Published(Strict):
    """The owner published the drafted tool; `toolId` is the id Conker sees in its tool list."""

    toolId: str = Field(pattern=TOOL_ID)
    name: str = Field(min_length=1, max_length=160)


def initialize(db):
    db.executescript(SCHEMA)


def status(request) -> str:
    return request["payload"].get("status") or READY


def _title(description: str) -> str:
    flat = " ".join(description.split())
    return "Draft a tool: " + (flat if len(flat) <= 120 else flat[:119].rstrip() + "…")


def drafting(description: str) -> dict:
    return {"description": description, "status": DRAFTING}


def failed(description: str, error: str) -> dict:
    return {"description": description, "status": FAILED, "error": error[:600]}


def file(store, description, *, session_id=None, turn_id=None) -> dict:
    """A new tool request, drafting from now on."""
    return change_requests.file(
        store,
        "tool_draft",
        _title(description),
        description[:600],
        drafting(description),
        session_id=session_id,
        turn_id=turn_id,
    )


def submit(store, body: NewDraft, *, now=None) -> dict:
    """A tool request without a chat, at most one per request_id."""
    digest = hashlib.sha256(body.description.encode("utf-8")).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM tool_draft_submissions WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if row is None:
            request = change_requests.file_in(
                db,
                "tool_draft",
                _title(body.description),
                body.description[:600],
                drafting(body.description),
                now=now,
            )
            db.execute(
                "INSERT INTO tool_draft_submissions VALUES (?,?,?,?)",
                (body.request_id, request["id"], digest, time.time() if now is None else now),
            )
            db.commit()
            return request
    if row["description_sha256"] != digest:
        raise ToolDraftError(
            "request_reused", "This request_id was already used for another description.", 409
        )
    return change_requests.get(store, row["change_request_id"])


def pending(store) -> list[dict]:
    """Open tool requests waiting to be drafted, oldest first."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM change_requests WHERE state='open' AND kind='tool_draft' "
            "AND json_extract(payload, '$.status')=? ORDER BY created_at",
            (DRAFTING,),
        ).fetchall()
    return [change_requests._view(row) for row in rows]


def _replace(store, request, payload, title, summary) -> bool:
    """Write a new payload while the request is still open and holds exactly what `request`
    holds: a decline or another attempt in the meantime wins."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT state, payload FROM change_requests WHERE id=?", (request["id"],)
        ).fetchone()
        if (
            row is None
            or row["state"] != "open"
            or json.loads(row["payload"]) != request["payload"]
        ):
            return False
        db.execute(
            "UPDATE change_requests SET payload=?, title=?, summary=? WHERE id=?",
            (change_requests.encode(payload), title[:160], summary[:600], request["id"]),
        )
        db.commit()
    return True


def still_drafting(store, request) -> bool:
    try:
        current = change_requests.get(store, request["id"])
    except change_requests.ChangeRequestError:
        return False
    return current["state"] == "open" and current["payload"] == request["payload"]


def finish(store, request, payload, title=None, summary=None) -> bool:
    """Record what drafting gave: a ready draft or the reason it failed."""
    try:
        change_requests.encode(payload)
    except change_requests.ChangeRequestError:
        payload = failed(request["payload"]["description"], "The draft is too large to review.")
        title = summary = None
    return _replace(
        store,
        request,
        payload,
        title or request["title"],
        request["summary"] if summary is None else summary,
    )


def retry(store, identity, body: Retry) -> dict:
    """Back to drafting, from a new description when he gave one."""
    request = change_requests.get(store, identity)
    if request["kind"] != "tool_draft":
        raise ToolDraftError("not_a_tool_draft", "Only a tool request is drafted again.", 409)
    if request["state"] != "open":
        raise ToolDraftError("decided", "This request was already decided.", 409)
    if status(request) == DRAFTING:
        raise ToolDraftError("drafting", "This tool is still being drafted.", 409)
    description = body.description or request["payload"].get("description") or request["summary"]
    if not _replace(store, request, drafting(description), _title(description), description[:600]):
        raise ToolDraftError("changed", "This request changed meanwhile. Reload it.", 409)
    return change_requests.get(store, identity)


def recover_interrupted(store) -> int:
    """A draft that was running when Pi stopped did not finish: say so on its card."""
    stopped = 0
    for request in pending(store):
        stopped += finish(store, request, failed(request["payload"]["description"], RESTARTED))
    return stopped


def published(store, identity, body: Published, now=None) -> tuple[dict, bool]:
    """Record that the owner published this request's tool. True the first time, when Conker
    tells the chat that asked; the request counts as done (approved) from then on."""
    now = time.time() if now is None else now
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM change_requests WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ToolDraftError("not_found", "Request not found.", 404)
        if row["kind"] != "tool_draft":
            raise ToolDraftError("not_a_tool_draft", "Only a tool request is published.", 409)
        if row["state"] not in ("open", "approved"):
            raise ToolDraftError("decided", "This request was already decided.", 409)
        result = json.loads(row["result"]) if row["result"] else {}
        before = result.get("published") or {}
        if before.get("toolId") == body.toolId:
            return change_requests._view(row), False
        result["published"] = {"toolId": body.toolId, "name": body.name, "publishedAt": now}
        db.execute(
            "UPDATE change_requests SET state='approved', result=?, "
            "decided_at=COALESCE(decided_at, ?) WHERE id=?",
            (json.dumps(result, ensure_ascii=False), now, identity),
        )
        db.commit()
        row = db.execute("SELECT * FROM change_requests WHERE id=?", (identity,)).fetchone()
    return change_requests._view(row), not before


def problem(issue: dict) -> str:
    """A ToolGate check issue, naming its step so the model knows where to look."""
    message = str(issue.get("message") or issue.get("code") or "Something is wrong.")[:300]
    return f"Step {issue['nodeId']}: {message}" if issue.get("nodeId") else message


def build(store, providers, description, capabilities, *, check, attempt, alive=lambda: True):
    """Draft one tool from Conker's capabilities: (payload, title, summary) for its request.

    `check(document)` is ToolGate's check (`{"valid", "issues"}`), or None when ToolGate could not
    check it; then the draft is ready with Pi's own issues and a note. `attempt` runs each model
    call (`workflow_drafts.draft`). `alive()` turns false when the request was declined or Pi is
    stopping, so no more repairs are asked for.
    """
    offered = draft_capabilities.relevant(capabilities, description)
    known = [
        workflow_drafts.Capability.model_validate(draft_capabilities.compact(item))
        for item in capabilities
    ]
    body = {
        "description": description,
        "capabilities": [draft_capabilities.compact(item) for item in offered],
    }

    def run(**more):
        return workflow_drafts.draft(
            store,
            providers,
            workflow_drafts.Request.model_validate({**body, **more}),
            minimum_timeout_ms=None,
            attempt=attempt,
            known=known,
        )

    try:
        result = run()
    except workflow_drafts.DraftError as failure:
        return failed(description, failure.message), None, None
    if not result["ready"] or result["document"] is None:
        return failed(description, "The drafter needs more detail: " + result["reply"]), None, None
    document, own, checked, issues = result["document"], result["issues"], True, []
    for repairs in range(REPAIRS + 1):
        found = check(document)
        if found is None:
            checked, issues = False, list(own)
            break
        issues = list(dict.fromkeys([*own, *(problem(item) for item in found["issues"])]))
        if (found["valid"] and not own) or repairs == REPAIRS or not alive():
            break
        try:
            again = run(draftId=document["id"], previous=document, issues=issues[:20])
        except Exception:
            # A repair that fails, runs out of time or is stopped keeps the draft it repaired.
            break
        if not again["ready"] or again["document"] is None:
            break
        result, document, own = again, again["document"], again["issues"]
    payload = {
        "description": description,
        "status": READY,
        "document": document,
        "issues": issues[:50],
        "steps": draft_capabilities.steps(document, capabilities),
        "checked": checked,
    }
    if not checked:
        payload["note"] = NOT_CHECKED
    return payload, f"Add the tool “{document['name']}”", result["reply"]
