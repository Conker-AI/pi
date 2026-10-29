"""The owner's side of change requests: see what Conker asked to create, then approve or decline.

Tool requests also start here: `POST /tool-drafts` files one without a chat, `retry` drafts one
again, and `published` records that its tool is live, which Conker says in the chat that asked.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import Field

from pi.agents import change_requests, tool_drafts
from pi.core.models import Strict
from pi.turns import answers, tool_drafting

RequestPath = Annotated[str, Path(pattern=change_requests.REQUEST_ID[1:-1].join("^$"))]


class Decline(Strict):
    reply: str = Field(default="", max_length=4000)


def _kick(loop):
    """Wake the drafting worker; without one (tests, a stopped app) the request waits for it."""
    drafts = getattr(loop(), "drafts", None)
    if drafts is not None:
        drafts.kick()


def drafts_router(store, authorize, loop=lambda: None):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.post("/tool-drafts")
    def new(body: tool_drafts.NewDraft):
        """File a tool request without a chat; drafting starts at once and the answer does not
        wait for it. The same request_id gives back the same request."""
        request = tool_drafts.submit(store(), body)
        _kick(loop)
        return request

    return routes


def router(store, authorize, memory_writer=lambda: None, loop=lambda: None):
    routes = APIRouter(prefix="/change-requests", dependencies=[Depends(authorize)])

    @routes.get("")
    def listing():
        """Open requests, newest first."""
        return {"schemaVersion": 1, "results": change_requests.listing(store())}

    @routes.get("/{identity}")
    def get(identity: RequestPath):
        return change_requests.get(store(), identity)

    @routes.post("/{identity}/approve")
    def approve(identity: RequestPath):
        """Make a cron job or memory real exactly as filed."""
        return change_requests.approve(store(), identity, memory=memory_writer())

    @routes.post("/{identity}/saved")
    def saved(identity: RequestPath, body: change_requests.DraftSaved):
        """The owner saved this tool request as a draft in Tools; recording it creates nothing."""
        return change_requests.saved(store(), identity, body)

    @routes.post("/{identity}/retry")
    def retry(identity: RequestPath, body: tool_drafts.Retry | None = None):
        """Draft this tool request again, from a new description when one is given."""
        request = tool_drafts.retry(store(), identity, body or tool_drafts.Retry())
        _kick(loop)
        return request

    @routes.post("/{identity}/published")
    def published(identity: RequestPath, body: tool_drafts.Published):
        """The owner published this request's tool. The first time, Conker says in the chat that
        asked that it is live (a stored message, not a model call)."""
        request, first = tool_drafts.published(store(), identity, body)
        if first and request["sessionId"]:
            tool_drafting.announce(store(), request["sessionId"], body.name)
        return request

    @routes.post("/{identity}/decline")
    def decline(identity: RequestPath, body: Decline | None = None):
        """Nothing is created; Conker is told it was declined if it asks again. Words he wrote
        with it (Change) go to that chat as his next message."""
        declined = change_requests.decline(store(), identity)
        words = (body.reply if body else "").strip()
        if words and declined["sessionId"]:
            answers.background(
                lambda: answers.deliver(loop(), declined["sessionId"], words, f"change-{identity}")
            )
        return declined

    return routes
