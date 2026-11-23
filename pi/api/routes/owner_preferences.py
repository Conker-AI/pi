"""Owner preference and settings routes with revision-checked saves.

Preferences (quiet hours, proactivity, daily budgets, plan alerts, idle timeout) and the owner's
settings document (`pi.core.owner_settings`) are the owner's own: the Settings screen reads and saves
them with the owner credential, and preference saves ask for his password at the gateway.

The daily spending cap (`spendingCap`) changes only with that password (R-M6). The owner credential
is the gateway, which asked for it (the browser's password proof, or the `spending-cap` host
command); a save on the recovery key (the host CLI's relay) that would change the cap is refused.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from pi.core import owner_preferences as owner_preferences
from pi.core import owner_settings as owner_settings
from pi.core.models import described


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
    settings_reply = described(owner_settings.OwnerSettingsDocument)
    preferences_reply = described(owner_preferences.OwnerPreferencesDocument)

    @routes.get("/owner/settings", dependencies=[Depends(require_owner)], responses=settings_reply)
    def get_owner_settings():
        return owner_settings.load(app_state().store)

    @routes.post("/owner/settings", dependencies=[Depends(require_owner)], responses=settings_reply)
    def save_owner_settings(body: owner_settings.Update):
        try:
            return owner_settings.save(app_state().store, body)
        except owner_settings.RevisionConflict as exc:
            raise _conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, {"code": "invalid_settings", "message": str(exc)}) from exc

    @routes.get(
        "/owner/preferences", dependencies=[Depends(require_owner)], responses=preferences_reply
    )
    def get_owner_preferences():
        return owner_preferences.load(app_state().store)

    @routes.post(
        "/owner/preferences", dependencies=[Depends(require_owner)], responses=preferences_reply
    )
    def save_owner_preferences(body: owner_preferences.UpdatePreferences, request: Request):
        # Anything but the owner credential (the recovery key) keeps the cap as stored.
        keep_cap = getattr(request.state, "owner_credential", False) is not True
        try:
            return owner_preferences.save(app_state().store, body, keep_cap=keep_cap)
        except owner_preferences.RevisionConflict as exc:
            raise _conflict(exc) from exc
        except owner_preferences.CapNeedsPassword as exc:
            raise HTTPException(
                403,
                {
                    "code": "password_required",
                    "message": "Change the daily spending cap in Settings or with "
                    "conker spending cap; both ask for your password.",
                },
            ) from exc

    return routes
