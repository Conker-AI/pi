"""Owner-only directory tree metadata; no file-content or write routes."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from . import filesystem_reads as reads
from . import filesystem_roots
from .system_actions import ActionError


def router(store, gate, authorize):
    routes = APIRouter(prefix="/system/files", dependencies=[Depends(authorize)])

    @routes.get("/roots")
    def roots():
        try:
            return JSONResponse(
                filesystem_roots.read(gate()), headers={"Cache-Control": "no-store"}
            )
        except ActionError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    def invoke(function, *args):
        try:
            return JSONResponse(
                function(store(), gate(), *args), headers={"Cache-Control": "no-store"}
            )
        except reads.FileReadError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.post("/listings")
    def request(body: reads.Read):
        return invoke(reads.request, body)

    @routes.get("/listings/{identity}")
    def inspect(identity: str):
        return invoke(reads.inspect, identity)

    @routes.post("/listings/{identity}/resume")
    def resume(identity: str):
        return invoke(reads.resume, identity)

    return routes
