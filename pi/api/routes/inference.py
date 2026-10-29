"""Model calls for Conker's services (docs/inference.md), and what they used."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from pi.models import service_inference


def router(app_state, require_owner):
    routes = APIRouter(prefix="/inference")

    def require_service(key: str | None = Header(None, alias="X-Pi-Service-Key")) -> str:
        service = service_inference.authenticate(
            getattr(app_state(), "service_key_hashes", {}) or {}, key
        )
        if service is None:
            raise HTTPException(401, "Missing or invalid service credential.")
        return service

    @routes.post(
        "",
        response_model=service_inference.InferenceAnswer,
        responses={
            401: {"description": "Missing or invalid service key"},
            403: {"description": "The role is not this service's"},
            503: {"description": "`no_model` or `unavailable`: the caller falls back"},
        },
    )
    def infer(body: service_inference.InferenceRequest, service: str = Depends(require_service)):
        """One prompt for one of the calling service's model roles."""
        state = app_state()
        return service_inference.call(state.store, state.router, service, body)

    @routes.get("/usage", dependencies=[Depends(require_owner)])
    def usage(limit: int = Query(default=50, ge=1, le=200)):
        """Recent service calls and the last day's totals; no prompt or answer text."""
        return service_inference.usage(app_state().store, limit)

    return routes
