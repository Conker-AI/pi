"""Compaction API routes."""

from fastapi import APIRouter, Depends, HTTPException

from pi.context import compaction


def router(store, authorize):
    routes = APIRouter(
        prefix="/context/{session_id}", tags=["compaction"], dependencies=[Depends(authorize)]
    )

    def invoke(fn, *args):
        try:
            return fn(store(), *args)
        except compaction.controls.ContextError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.post("/compact")
    def compact(session_id: str, body: compaction.Compact):
        """Compact a conversation by folding messages into the summary."""
        return invoke(compaction.compact, session_id, body)

    @routes.get("/compaction")
    def view(session_id: str):
        """View the current compaction state."""
        return invoke(compaction.view, session_id)

    return routes
