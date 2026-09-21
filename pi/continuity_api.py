from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from . import continuity, proactive_budget


def router(store, authorize):
    routes = APIRouter(prefix="/continuity", dependencies=[Depends(authorize)])

    @routes.get("/budget")
    def budget():
        return JSONResponse(proactive_budget.status(store()), headers={"Cache-Control": "no-store"})

    @routes.post("/budget/reserve")
    def reserve(body: proactive_budget.Request):
        try:
            value = proactive_budget.reserve(store(), body)
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except proactive_budget.Denied as exc:
            return JSONResponse(
                {"code": "proactive_admission_denied", "reasons": exc.reasons},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

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
