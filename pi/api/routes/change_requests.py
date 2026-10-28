"""The owner's side of change requests: see what Conker asked to create, then approve or decline."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import Field

from pi.agents import change_requests
from pi.core.models import Strict
from pi.turns import answers

RequestPath = Annotated[str, Path(pattern=change_requests.REQUEST_ID[1:-1].join("^$"))]


class Decline(Strict):
    reply: str = Field(default="", max_length=4000)


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
