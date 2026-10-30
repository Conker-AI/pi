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

When the drafter needs a detail, a request from a chat asks there instead of failing: the payload
becomes `{"description", "status": "needs_detail", "question"}` (plus `conversation`) and Conker
posts the question in that chat (`askedIn` is that chat once it is posted, see
`pi.turns.tool_drafting.ask`). The owner's next message in that chat, or his answer from the Inbox
(`retry` with `answer`), goes on with the same draft: the drafter sees the questions and answers
so far (`conversation`, the last `CONVERSATION` entries) and asks at most `QUESTIONS` times in
all. A request without a chat fails with the drafter's question, as before.
"""

from __future__ import annotations

import hashlib
import json
import time

from pydantic import Field, field_validator, model_validator

from pi.agents import change_requests, draft_capabilities, workflow_drafts
from pi.core.errors import ServiceError
from pi.core.models import Strict

DRAFTING, READY, FAILED, NEEDS_DETAIL = "drafting", "ready", "failed", "needs_detail"
RESTARTED = "Pi restarted while drafting. Try again."
QUESTIONS = 4  # questions the drafter may ask about one request before it fails
CONVERSATION = 16  # questions and answers the drafter sees (`workflow_drafts.Request`)
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
    """Draft it again: from a new description, which starts over; with the owner's answer to the
    drafter's question, which goes on with the same draft; or as it was, answers kept."""

    description: str | None = Field(default=None, min_length=1, max_length=4000)
    answer: str | None = Field(default=None, min_length=1, max_length=4000)

    _description = field_validator("description", "answer")(_words)

    @model_validator(mode="after")
    def one(self):
        if self.description is not None and self.answer is not None:
            raise ValueError("Give a new description or an answer, not both.")
        return self


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


def _with(payload: dict, conversation) -> dict:
    return {**payload, "conversation": list(conversation)} if conversation else payload


def drafting(description: str, conversation=()) -> dict:
    return _with({"description": description, "status": DRAFTING}, conversation)


def failed(description: str, error: str, conversation=()) -> dict:
    return _with({"description": description, "status": FAILED, "error": error[:600]}, conversation)


def needs_detail(description: str, question: str, conversation=()) -> dict:
    return _with(
        {"description": description, "status": NEEDS_DETAIL, "question": question[:2000]},
        conversation,
    )


def conversation(request) -> list[dict]:
    """The drafter's questions and the owner's answers so far, oldest first."""
    return list(request["payload"].get("conversation") or [])


def answered(request, answer: str) -> dict:
    """The payload that goes on drafting with the owner's answer to the drafter's question."""
    payload = request["payload"]
    said = [
        *conversation(request),
        {"role": "builder", "content": payload["question"][:4000]},
        {"role": "owner", "content": answer.strip()[:4000]},
    ]
    return drafting(payload["description"], said[-CONVERSATION:])


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
    """Back to drafting: from a new description when he gave one (a fresh start), with his answer
    to the drafter's question (the same draft goes on), or as it was, answers kept."""
    request = change_requests.get(store, identity)
    if request["kind"] != "tool_draft":
        raise ToolDraftError("not_a_tool_draft", "Only a tool request is drafted again.", 409)
    if request["state"] != "open":
        raise ToolDraftError("decided", "This request was already decided.", 409)
    if status(request) == DRAFTING:
        raise ToolDraftError("drafting", "This tool is still being drafted.", 409)
    if body.answer is not None:
        if status(request) != NEEDS_DETAIL:
            raise ToolDraftError("not_asking", "The drafter has no question waiting.", 409)
        payload = answered(request, body.answer)
    elif body.description is not None:
        payload = drafting(body.description)
    else:
        description = request["payload"].get("description") or request["summary"]
        payload = drafting(description, conversation(request))
    description = payload["description"]
    if not _replace(store, request, payload, _title(description), description[:600]):
        raise ToolDraftError("changed", "This request changed meanwhile. Reload it.", 409)
    return change_requests.get(store, identity)


def asking(store) -> list[dict]:
    """Open tool requests whose question waits for the owner, newest first."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM change_requests WHERE state='open' AND kind='tool_draft' "
            "AND json_extract(payload, '$.status')=? ORDER BY created_at DESC",
            (NEEDS_DETAIL,),
        ).fetchall()
    return [change_requests._view(row) for row in rows]


def mark_asked(db, request, chat_id) -> bool:
    """Inside the caller's transaction: the question is posted in `chat_id`. False when the
    request changed meanwhile (answered from the Inbox, declined): then nothing is posted."""
    row = db.execute(
        "SELECT state, payload FROM change_requests WHERE id=?", (request["id"],)
    ).fetchone()
    if row is None or row["state"] != "open" or json.loads(row["payload"]) != request["payload"]:
        return False
    db.execute(
        "UPDATE change_requests SET payload=? WHERE id=?",
        (change_requests.encode({**request["payload"], "askedIn": chat_id}), request["id"]),
    )
    return True


def take_answer(store, request, answer: str) -> bool:
    """He answered the drafter's question in the chat: drafting goes on. False when the request
    changed meanwhile, so his message stays an ordinary message."""
    description = request["payload"]["description"]
    return _replace(
        store, request, answered(request, answer), _title(description), request["summary"]
    )


def recover_interrupted(store) -> int:
    """A draft that was running when Pi stopped did not finish: say so on its card."""
    stopped = 0
    for request in pending(store):
        description = request["payload"]["description"]
        stopped += finish(store, request, failed(description, RESTARTED, conversation(request)))
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


def build(
    store,
    providers,
    description,
    capabilities,
    *,
    check,
    attempt,
    alive=lambda: True,
    conversation=(),
    ask=False,
):
    """Draft one tool from Conker's capabilities: (payload, title, summary) for its request.

    `check(document)` is ToolGate's check (`{"valid", "issues"}`), or None when ToolGate could not
    check it; then the draft is ready with Pi's own issues and a note. `attempt` runs each model
    call (`workflow_drafts.draft`). `alive()` turns false when the request was declined or Pi is
    stopping, so no more repairs are asked for. `conversation` holds the drafter's questions and
    the owner's answers so far; with `ask` a question waits for the chat that asked
    (`needs_detail`) instead of failing the request.
    """
    said = list(conversation)[-CONVERSATION:]
    offered = draft_capabilities.relevant(capabilities, description)
    known = [
        workflow_drafts.Capability.model_validate(draft_capabilities.compact(item))
        for item in capabilities
    ]
    body = {
        "description": description,
        "conversation": said,
        "capabilities": [draft_capabilities.compact(item) for item in offered],
    }

    def run(**more):
        return workflow_drafts.draft(
            store,
            providers,
            workflow_drafts.Request.model_validate({**body, **more}),
            attempt=attempt,
            known=known,
        )

    try:
        result = run()
    except workflow_drafts.DraftError as failure:
        return failed(description, failure.message, said), None, None
    if not result["ready"] or result["document"] is None:
        asked = sum(item.get("role") == "builder" for item in said)
        if ask and asked < QUESTIONS:
            return needs_detail(description, result["reply"], said), None, None
        reason = (
            "The drafter still has a question: " if asked else "The drafter needs more detail: "
        )
        return failed(description, reason + result["reply"], said), None, None
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
    payload = _with(
        {
            "description": description,
            "status": READY,
            "document": document,
            "issues": issues[:50],
            "steps": draft_capabilities.steps(document, capabilities),
            "checked": checked,
        },
        said,
    )
    if not checked:
        payload["note"] = NOT_CHECKED
    return payload, f"Add the tool “{document['name']}”", result["reply"]
