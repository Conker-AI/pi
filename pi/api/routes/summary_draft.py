from fastapi import APIRouter, Depends

from pi.context import summary_draft


def router(store, providers, authorize):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.post("/context/{session_id}/summary/draft")
    def draft(session_id: str, body: summary_draft.Request):
        """A proposed summary for review; saves nothing."""
        return summary_draft.draft(store(), providers(), session_id, body)

    return routes
