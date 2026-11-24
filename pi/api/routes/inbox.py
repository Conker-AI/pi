"""Inbox API route, and the web push claim over the same Inbox items."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from pi.agents import inbox as inbox_agent
from pi.agents import push_queue


def router(store, authorize, owner):
    routes = APIRouter()

    @routes.get("/inbox", dependencies=[Depends(authorize)])
    def get_inbox():
        """Get the inbox view of everything that happened and everything that needs the owner."""
        return inbox_agent.inbox(store())

    # The gateway's own call with the owner-control key; its browser relay refuses this route
    # (browser_contract.GATEWAY_ONLY).
    @routes.post("/push/claim", dependencies=[Depends(owner)])
    def claim_push(body: push_queue.Claim):
        """Claim the Inbox items that may wake the owner's devices now; each is claimed once."""
        return JSONResponse(push_queue.claim(store(), body), headers={"Cache-Control": "no-store"})

    return routes
