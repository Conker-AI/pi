"""What ran without asking the owner, by his access settings: Inbox Handled."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from pi.turns import access


def router(store, authorize):
    routes = APIRouter(prefix="/access", dependencies=[Depends(authorize)])

    @routes.get("/handled")
    def handled():
        """The last week of actions that ran without asking him, newest first, with who decided."""
        return {"schemaVersion": 1, "results": access.handled(store(), time.time() - 7 * 86400)}

    return routes
