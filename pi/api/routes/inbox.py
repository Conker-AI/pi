"""Inbox API route, text shared from another app, and the web push claim over the Inbox items."""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse

from pi.agents import inbox as inbox_agent
from pi.agents import push_queue, shared_notes


def router(store, authorize, owner):
    routes = APIRouter()

    @routes.get("/inbox", dependencies=[Depends(authorize)])
    def get_inbox():
        """Get the inbox view of everything that happened and everything that needs the owner."""
        return inbox_agent.inbox(store())

    @routes.post("/inbox/shared", dependencies=[Depends(owner)], response_model=shared_notes.Shared)
    def save_shared(body: shared_notes.Share):
        """Save shared text in a new chat of its own, marked outside, with an Inbox note; no
        model runs. The same request id returns the same chat."""
        return shared_notes.save(store(), body)

    @routes.get(
        "/inbox/shared",
        dependencies=[Depends(owner)],
        response_model=shared_notes.SharedNoteCollection,
    )
    def shared():
        """Shared text still in the Inbox, newest first, until dismissed."""
        return {"results": cast(Any, shared_notes.notes(store()))}

    @routes.post("/inbox/shared/{identity}/dismiss", dependencies=[Depends(owner)])
    def dismiss_shared(identity: Annotated[str, Path(pattern=shared_notes.NOTE_ID)]):
        """Hide the note; the chat keeps the text."""
        return shared_notes.dismiss(store(), identity)

    # The gateway's own call with the owner-control key; its browser relay refuses this route
    # (browser_contract.GATEWAY_ONLY).
    @routes.post("/push/claim", dependencies=[Depends(owner)])
    def claim_push(body: push_queue.Claim):
        """Claim the Inbox items that may wake the owner's devices now; each is claimed once."""
        return JSONResponse(push_queue.claim(store(), body), headers={"Cache-Control": "no-store"})

    return routes
