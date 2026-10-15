"""Memory delivery status and owner-reviewed forgetting of one memory."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from pi.memory import forget as memory_forget


def router(app_state, require_key, require_owner):
    routes = APIRouter()

    @routes.get("/memory/forget-pending", dependencies=[Depends(require_owner)])
    def pending_memory_forget():
        return memory_forget.pending(app_state().store)

    @routes.get("/memory/forget/{memory_id}", dependencies=[Depends(require_owner)])
    def preview_memory_forget(memory_id: str):
        try:
            return memory_forget.preview(
                getattr(app_state(), "memory_corrections", None), memory_id
            )
        except memory_forget.ForgetError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.post("/memory/forget", dependencies=[Depends(require_owner)])
    def forget_memory(body: memory_forget.Forget):
        """Remove one memory everywhere it is stored. The source chat is kept."""
        try:
            return memory_forget.forget(
                app_state().store, getattr(app_state(), "memory_corrections", None), body
            )
        except memory_forget.ForgetError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.get("/memory", dependencies=[Depends(require_key)])
    def memory_status():
        """Delivery progress is server state; a model cannot hide this notice."""
        return app_state().loop.memory.status()

    return routes
