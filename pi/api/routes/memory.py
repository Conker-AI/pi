"""Memory delivery status, owner-reviewed forgetting of one memory, and adding one by hand."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query

from pi.core.models import described
from pi.memory import beliefs as memory_beliefs
from pi.memory import forget as memory_forget
from pi.memory import memory_replies as replies
from pi.memory import stated as stated_memory
from pi.memory import upkeep as memory_upkeep
from pi.memory import uses as memory_uses


def router(app_state, require_key, require_owner):
    routes = APIRouter()

    @routes.get(
        "/memory/forget-pending",
        dependencies=[Depends(require_owner)],
        responses=described(replies.MemoryForgetPending),
    )
    def pending_memory_forget():
        return memory_forget.pending(app_state().store)

    @routes.get("/memory/forget/{memory_id}", dependencies=[Depends(require_owner)])
    def preview_memory_forget(memory_id: str):
        state = app_state()
        return memory_forget.preview(
            getattr(state, "memory_corrections", None), memory_id, state.store
        )

    @routes.post(
        "/memory/forget",
        dependencies=[Depends(require_owner)],
        responses=described(replies.MemoryForgetReceipt),
    )
    def forget_memory(body: memory_forget.Forget):
        """Remove one memory everywhere it is stored. The source chat is kept."""
        return memory_forget.forget(
            app_state().store, getattr(app_state(), "memory_corrections", None), body
        )

    @routes.post(
        "/memory/forget-many",
        dependencies=[Depends(require_owner)],
        responses=described(replies.MemoryForgetManyResult),
    )
    def forget_memories(body: memory_forget.ForgetMany):
        """Forget up to 50 memories one at a time; stops at the first refusal."""
        return memory_forget.forget_many(
            app_state().store, getattr(app_state(), "memory_corrections", None), body
        )

    @routes.post(
        "/memory/remember",
        dependencies=[Depends(require_owner)],
        responses=described(replies.MemoryRemembered),
    )
    def remember_memory(body: stated_memory.Remember):
        """Add one memory in the owner's own words; the same request id never stores it twice."""
        return stated_memory.remember(getattr(app_state(), "memory_corrections", None), body)

    @routes.get(
        "/memory/beliefs",
        dependencies=[Depends(require_owner)],
        responses=described(memory_beliefs.BeliefPage),
    )
    def list_beliefs(
        origin: Literal["stated", "concluded", "all"] = "all",
        state: Literal["current", "needs_review", "all"] = "all",
        memory_type: Literal["fact", "phase", "context", "watch"] | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, min_length=1, max_length=600),
    ):
        """What Conker believes about the owner, one page at a time (read only).

        A memory with a forget pending or done, or made from a hidden chat, is left out; `total`
        and `counts` drop only by the rows left out of this page."""
        return memory_beliefs.beliefs(
            app_state().memory,
            {
                "origin": origin,
                "state": state,
                "memory_type": memory_type,
                "limit": limit,
                "after": after,
            },
        )

    @routes.get(
        "/memory/upkeep",
        dependencies=[Depends(require_owner)],
        responses=described(memory_upkeep.UpkeepPage),
    )
    def list_upkeep(
        unused_days: int = Query(default=90, ge=1, le=3650),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        """Upkeep candidates (duplicates, open contradictions, expired, not recalled), read only.

        A candidate naming a memory with a forget pending or done, or one made from a hidden chat,
        is left out whole; each kind's `total` drops only by the items left out of this reply."""
        return memory_upkeep.upkeep(
            app_state().memory, {"unused_days": unused_days, "limit": limit}
        )

    @routes.get(
        "/memory/{memory_id}/uses",
        dependencies=[Depends(require_owner)],
        responses=described(memory_uses.UsePage),
    )
    def list_memory_uses(
        memory_id: str = Path(max_length=memory_uses.MAX_ID),
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, min_length=1, max_length=200),
    ):
        """The answers that used one memory, newest first (read only, ids and chat titles).

        Works for a memory since corrected or forgotten: the notes hold no words. Chats that are
        forgotten or in the Trash are left out. `since` is when Pi began noting uses."""
        return memory_uses.listing(app_state().store, memory_id, limit, after)

    @routes.get("/memory", dependencies=[Depends(require_key)])
    def memory_status():
        """Delivery progress is server state; a model cannot hide this notice."""
        return app_state().loop.memory.status()

    return routes
