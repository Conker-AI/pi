"""Owner preference routes with revision-checked saves."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from . import owner_preferences


def router(app_state, require_admin):
    routes = APIRouter()

    @routes.get("/owner/preferences", dependencies=[Depends(require_admin)])
    def get_owner_preferences():
        return owner_preferences.load(app_state().store)

    @routes.post("/owner/preferences", dependencies=[Depends(require_admin)])
    def save_owner_preferences(body: owner_preferences.UpdatePreferences):
        try:
            return owner_preferences.save(app_state().store, body)
        except owner_preferences.RevisionConflict as exc:
            raise HTTPException(
                409,
                {
                    "code": "revision_conflict",
                    "message": str(exc),
                    "current_revision": exc.current_revision,
                },
            ) from exc

    return routes
