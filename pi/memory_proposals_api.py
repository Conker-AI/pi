from fastapi import APIRouter, Depends

from . import memory_proposals as proposals


def router(store, client, authorize):
    routes = APIRouter(prefix="/memory-proposals", dependencies=[Depends(authorize)])

    @routes.get("")
    def listing(session_id: str):
        return proposals.list_all(store(), session_id)

    @routes.post("")
    def create(body: proposals.Create):
        return proposals.create(store(), body, client())

    @routes.get("/{identity}")
    def get(identity: str):
        return proposals.get(store(), identity)

    @routes.post("/{identity}/decision")
    def decide(identity: str, body: proposals.Decision):
        return proposals.decide(store(), identity, body, client())

    @routes.post("/{identity}/reconcile")
    def reconcile(identity: str):
        return proposals.reconcile(store(), identity, client())

    return routes
