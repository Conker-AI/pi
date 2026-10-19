"""Compaction API routes."""

from fastapi import APIRouter, Depends

from pi.context import compaction


def router(store, authorize):
    routes = APIRouter(
        prefix="/context/{session_id}", tags=["compaction"], dependencies=[Depends(authorize)]
    )

    def invoke(fn, *args):
        return fn(store(), *args)

    @routes.post("/compact")
    def compact(session_id: str, body: compaction.Compact):
        """Compact a conversation by folding messages into the summary."""
        return invoke(compaction.compact, session_id, body)

    @routes.get("/compaction")
    def view(session_id: str):
        """View the current compaction state."""
        return invoke(compaction.view, session_id)

    return routes
