"""Redacted owner inventory reads through Pi's fixed ToolGate capability."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from . import system_inventory as inventory
from . import system_targets
from .system_actions import ActionError


def router(store, gate, authorize):
    routes = APIRouter(prefix="/system/inventory", dependencies=[Depends(authorize)])

    def invoke(function, *args):
        try:
            return function(store(), gate(), *args)
        except inventory.InventoryError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    RequestPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{16,100}$")]

    def no_store(response):
        response.headers["Cache-Control"] = "no-store"

    def exact(request):
        if request.url.query:
            raise HTTPException(422, "Inventory routes do not accept query parameters.")

    @routes.get("/configured/services", response_model=inventory.ConfiguredTargetsView)
    def configured_services(request: Request, response: Response):
        exact(request)
        no_store(response)
        try:
            return inventory.configured_targets(
                store(), system_targets.read(gate(), services=True), "services"
            )
        except ActionError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.get("/configured/containers", response_model=inventory.ConfiguredTargetsView)
    def configured_containers(request: Request, response: Response):
        exact(request)
        no_store(response)
        try:
            return inventory.configured_targets(store(), system_targets.read(gate()), "containers")
        except ActionError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.post("", response_model=inventory.InventoryView)
    def create(body: inventory.Read, request: Request, response: Response):
        exact(request)
        no_store(response)
        return inventory.browser_view(store(), invoke(inventory.request, body))

    @routes.get("/{identity}", response_model=inventory.InventoryView)
    def inspect(identity: RequestPath, request: Request, response: Response):
        exact(request)
        no_store(response)
        return inventory.browser_view(store(), invoke(inventory.inspect, identity))

    @routes.post("/{identity}/resume", response_model=inventory.InventoryView)
    def resume(identity: RequestPath, request: Request, response: Response):
        exact(request)
        no_store(response)
        return inventory.browser_view(store(), invoke(inventory.resume, identity))

    return routes
