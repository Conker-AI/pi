"""Narrow owner-only memory correction surface; never a generic memory proxy."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from pi.memory import delivery as memory_delivery
from pi.memory import edits


class BoundedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            if request.scope["query_string"]:
                return JSONResponse(
                    {
                        "detail": {
                            "code": "invalid_request",
                            "message": "Query parameters are not supported.",
                        }
                    },
                    status_code=422,
                )
            if request.method == "POST":
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 128000:
                        return JSONResponse(
                            {
                                "detail": {
                                    "code": "invalid_request",
                                    "message": "Correction body exceeds its limit.",
                                }
                            },
                            status_code=413,
                        )
                request._body = bytes(raw)
            try:
                return await handler(request)
            except edits.EditError as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status)

        return bounded


def router(store, client, companion, authorize):
    routes = APIRouter(
        prefix="/memory/corrections", dependencies=[Depends(authorize)], route_class=BoundedRoute
    )

    def scoped_client():
        value, memory = client(), companion()
        if value is None or memory is None:
            edits._configured(None)
        if (value.agent_id, value.destination) != (
            memory.agent_id,
            memory_delivery.destination_digest(memory.http.base_url),
        ):
            raise edits.EditError(
                "not_configured",
                "Correction must target the configured Companion memory namespace.",
                503,
            )
        return value

    @routes.get("/pending")
    def pending():
        return edits.pending(store())

    @routes.get("/memories/{identity}")
    def preview(identity: str):
        return edits.preview(scoped_client(), identity)

    @routes.post("")
    def apply(body: edits.Apply):
        return edits.apply(store(), scoped_client(), body)

    @routes.get("/{identity}")
    def status(identity: str):
        return edits.status(store(), scoped_client(), identity)

    @routes.post("/{identity}/retry")
    def retry(identity: str, body: edits.Retry):
        return edits.retry(store(), scoped_client(), identity, body)

    return routes
