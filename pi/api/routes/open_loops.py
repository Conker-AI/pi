"""Open loops across chats (R-M7): list, add, and mark done, dropped or open again."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query

from pi.context import open_loops
from pi.core.models import described


def router(store, require_owner):
    routes = APIRouter(dependencies=[Depends(require_owner)])

    @routes.get("/open-loops", responses=described(open_loops.LoopPage))
    def list_open_loops(
        state: Literal["open", "done", "dropped", "all"] = "open",
        due_before: date | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, min_length=1, max_length=64),
    ):
        """Promises in visible chats, soonest due first and undated last (read only).

        `today` is the owner's local date, in the zone the quiet hours use; `overdue` compares
        an open loop's due date with it."""
        return open_loops.listing(store(), state, due_before, limit, after)

    @routes.post("/open-loops", responses=described(open_loops.LoopReceipt))
    def add_open_loop(body: open_loops.LoopAdd):
        """File one promise for a visible chat, optionally pointing at one of its messages."""
        return open_loops.add(store(), body)

    @routes.post("/open-loops/{loop_id}/state", responses=described(open_loops.LoopReceipt))
    def change_open_loop_state(loop_id: str, body: open_loops.LoopState):
        """Mark a promise done, dropped or open again, also in a forked or closed chat."""
        return open_loops.change_state(store(), loop_id, body)

    return routes
