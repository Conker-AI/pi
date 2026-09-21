"""Owner-only telemetry reads; no browser-to-host transport."""

from fastapi import APIRouter, Depends, HTTPException

from . import system_inventory as inventory


def router(store, gate, authorize):
    routes = APIRouter(prefix="/system/inventory", dependencies=[Depends(authorize)])

    def invoke(function, *args):
        try:
            return function(store(), gate(), *args)
        except inventory.InventoryError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.post("")
    def request(body: inventory.Read):
        return invoke(inventory.request, body)

    @routes.get("/{identity}")
    def inspect(identity: str):
        return invoke(inventory.inspect, identity)

    @routes.post("/{identity}/resume")
    def resume(identity: str):
        return invoke(inventory.resume, identity)

    return routes
