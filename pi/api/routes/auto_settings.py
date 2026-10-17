from fastapi import APIRouter, Depends, HTTPException

from pi.models import auto_settings


def router(store, providers, authorize):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.post("/models/auto-settings")
    def propose(body: auto_settings.Request):
        """Proposed settings for the next message; saves and grants nothing."""
        try:
            return auto_settings.propose(store(), providers(), body)
        except auto_settings.ProposalError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    return routes
