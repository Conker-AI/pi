"""Next-message suggestions: Jev suggests what the owner might send next in one chat."""

from fastapi import APIRouter, Depends

from pi.conversation import suggestions


def router(get_store, get_providers, authorize):
    routes = APIRouter(prefix="/sessions", dependencies=[Depends(authorize)])

    @routes.post("/{session_id}/suggestions")
    def suggest(session_id: str, body: suggestions.Ask):
        return suggestions.suggest(get_store(), get_providers(), session_id, body)

    return routes
