"""Owner preference and settings routes with revision-checked saves.

Preferences (quiet hours, proactivity, daily budgets, plan alerts, idle timeout) and the owner's
settings document (`pi.owner.settings`) are the owner's own: the Settings screen reads and saves
them with the owner credential, and preference saves ask for his password at the gateway.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from pi.owner import preferences as owner_preferences
from pi.owner import settings as owner_settings


def _conflict(exc):
    return HTTPException(
        409,
        {
            "code": "revision_conflict",
            "message": str(exc),
            "current_revision": exc.current_revision,
        },
    )


def router(app_state, require_owner):
    routes = APIRouter()

    @routes.get("/owner/settings", dependencies=[Depends(require_owner)])
    def get_owner_settings():
        return owner_settings.load(app_state().store)

    @routes.post("/owner/settings", dependencies=[Depends(require_owner)])
    def save_owner_settings(body: owner_settings.Update):
        try:
            return owner_settings.save(app_state().store, body)
        except owner_settings.RevisionConflict as exc:
            raise _conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, {"code": "invalid_settings", "message": str(exc)}) from exc

    @routes.get("/owner/preferences", dependencies=[Depends(require_owner)])
    def get_owner_preferences():
        return owner_preferences.load(app_state().store)

    @routes.post("/owner/preferences", dependencies=[Depends(require_owner)])
    def save_owner_preferences(body: owner_preferences.UpdatePreferences):
        try:
            return owner_preferences.save(app_state().store, body)
        except owner_preferences.RevisionConflict as exc:
            raise _conflict(exc) from exc

    return routes
