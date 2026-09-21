"""Owner system controls use the existing gate approval path, never direct Docker."""

from fastapi import APIRouter, Depends, HTTPException, Query

from . import system_actions as actions


def router(store, gate, authorize):
    routes = APIRouter(prefix="/system/actions", dependencies=[Depends(authorize)])

    def invoke(function, *args):
        try:
            return function(store(), gate(), *args)
        except actions.ActionError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.post("")
    def request(body: actions.Request):
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

    return routes
