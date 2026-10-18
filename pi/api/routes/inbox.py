"""Inbox API route."""

from fastapi import APIRouter, Depends

from pi.agents import inbox as inbox_agent


def router(store, authorize):
    routes = APIRouter(prefix="/inbox", dependencies=[Depends(authorize)])

    @routes.get("")
    def get_inbox():
        """Get the inbox view of everything that happened and everything that needs the owner."""
        return inbox_agent.inbox(store())

    return routes
