from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from . import continuity


def router(store, authorize):
    routes = APIRouter(prefix="/continuity", dependencies=[Depends(authorize)])

    @routes.get("")
    def read(
        limit: int = Query(default=30, ge=1, le=100), before: int | None = Query(default=None, ge=1)
    ):
        return continuity.briefing(store(), limit=limit, before=before)

    @routes.post("/seen")
    def seen(body: continuity.Acknowledge):
        return continuity.acknowledge(store(), body)

    @routes.get("/notifications")
    def notifications(
        limit: int = Query(default=30, ge=1, le=100), before: int | None = Query(default=None, ge=1)
    ):
        return JSONResponse(
            continuity.notifications(store(), limit=limit, before=before),
            headers={"Cache-Control": "no-store"},
        )

    @routes.post("/notifications/delivered")
    def delivered(body: continuity.Acknowledge):
        return JSONResponse(
            continuity.delivered(store(), body), headers={"Cache-Control": "no-store"}
        )

    return routes
