"""Owner-only directory tree metadata; no file-content or write routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from . import filesystem_reads as reads
from . import filesystem_roots
from .system_actions import ActionError


def router(store, gate, authorize):
    routes = APIRouter(prefix="/system/files", dependencies=[Depends(authorize)])

    RequestPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{16,100}$")]

    def exact(request):
        if request.url.query:
            raise HTTPException(422, "File routes do not accept query parameters.")

    def no_store(response):
        response.headers["Cache-Control"] = "no-store"

    @routes.get("/roots", response_model=reads.RootCatalogue)
    def roots(request: Request, response: Response):
        exact(request)
        no_store(response)
        try:
            return filesystem_roots.browser_view(filesystem_roots.read(gate()))
        except ActionError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    def invoke(function, *args):
        try:
            return reads.browser_view(function(store(), gate(), *args))
        except reads.FileReadError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.post("/listings", response_model=reads.DirectoryView)
    def request(body: reads.Read, request: Request, response: Response):
        exact(request)
        no_store(response)
        return invoke(reads.request, body)

    @routes.get("/listings/{identity}", response_model=reads.DirectoryView)
    def inspect(identity: RequestPath, request: Request, response: Response):
        exact(request)
        no_store(response)
        return invoke(reads.inspect, identity)

    @routes.post("/listings/{identity}/resume", response_model=reads.DirectoryView)
    def resume(identity: RequestPath, request: Request, response: Response):
        exact(request)
        no_store(response)
        return invoke(reads.resume, identity)

    return routes
