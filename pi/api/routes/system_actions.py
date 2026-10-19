"""Owner system controls use the existing gate approval path, never direct Docker."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from pi.host import actions
from pi.host import port_finalization as system_port_finalization
from pi.host import port_recovery as system_port_recovery
from pi.host import port_reviews as system_port_reviews
from pi.host import targets as system_targets


def router(store, gate, authorize):
    routes = APIRouter(prefix="/system/actions", dependencies=[Depends(authorize)])

    def invoke(function, *args):
        return function(store(), gate(), *args)

    @routes.post("")
    def request(body: actions.Request):
        return invoke(actions.request, body)

    @routes.post("/services")
    def request_service(body: actions.ServiceRequest):
        return invoke(actions.request, body)

    @routes.post("/ports")
    def request_ports(body: actions.PortRequest):
        return invoke(actions.request, body)

    @routes.get("")
    def history(limit: int = Query(default=50, ge=1, le=100)):
        return {"results": actions.history(store(), limit)}

    @routes.get("/{identity}")
    def inspect(identity: str):
        return invoke(actions.inspect, identity)

    @routes.post("/{identity}/resume")
    def resume(identity: str):
        return invoke(actions.resume, identity)

    @routes.get("/{identity}/recovery")
    def recovery(identity: str):
        return JSONResponse(
            invoke(system_port_recovery.inspect, identity), headers={"Cache-Control": "no-store"}
        )

    @routes.post("/{identity}/recovery/finalize")
    def finalize(identity: str, body: system_port_finalization.Request):
        return JSONResponse(
            invoke(system_port_finalization.finalize, identity, body),
            headers={"Cache-Control": "no-store"},
        )

    return routes


def targets_router(gate, authorize):
    routes = APIRouter(prefix="/system", dependencies=[Depends(authorize)])

    @routes.post("/port-reviews")
    def create_port_review(body: system_port_reviews.Request):
        return JSONResponse(
            system_port_reviews.fetch(gate(), request=body),
            headers={"Cache-Control": "no-store"},
        )

    @routes.get("/port-reviews/{review_id}")
    def get_port_review(review_id: str):
        return JSONResponse(
            system_port_reviews.fetch(gate(), review_id=review_id),
            headers={"Cache-Control": "no-store"},
        )

    @routes.get("/targets")
    def targets():
        return JSONResponse(system_targets.read(gate()), headers={"Cache-Control": "no-store"})

    @routes.get("/services")
    def services():
        return JSONResponse(
            system_targets.read(gate(), services=True), headers={"Cache-Control": "no-store"}
        )

    return routes
