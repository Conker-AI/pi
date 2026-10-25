"""The owner's side of change requests: see what Conker asked to create, then approve or decline."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path

from pi.agents import change_requests

RequestPath = Annotated[str, Path(pattern=change_requests.REQUEST_ID[1:-1].join("^$"))]


def router(store, authorize, memory_writer=lambda: None):
    routes = APIRouter(prefix="/change-requests", dependencies=[Depends(authorize)])

    @routes.get("")
    def listing():
        """Open requests, newest first."""
        return {"schemaVersion": 1, "results": change_requests.listing(store())}

    @routes.get("/{identity}")
    def get(identity: RequestPath):
        return change_requests.get(store(), identity)

    @routes.post("/{identity}/approve")
    def approve(
        identity: RequestPath,
        body: Annotated[change_requests.DraftSaved | None, Body()] = None,
    ):
        """Make it real exactly as filed. A tool draft must already be saved in Tools."""
        return change_requests.approve(store(), identity, draft=body, memory=memory_writer())

    @routes.post("/{identity}/decline")
    def decline(identity: RequestPath):
        """Nothing is created; Conker is told it was declined if it asks again."""
        return change_requests.decline(store(), identity)

    return routes
