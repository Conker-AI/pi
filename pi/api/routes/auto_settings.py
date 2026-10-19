from fastapi import APIRouter, Depends

from pi.models import auto_settings


def router(store, providers, authorize):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.post("/models/auto-settings")
    def propose(body: auto_settings.Request):
        """Proposed settings for the next message; saves and grants nothing."""
        return auto_settings.propose(store(), providers(), body)

    return routes
