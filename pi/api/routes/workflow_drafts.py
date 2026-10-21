from fastapi import APIRouter, Depends

from pi.agents import workflow_drafts


def router(store, providers, authorize):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.post("/workflow-drafts/draft")
    def draft(body: workflow_drafts.Request):
        """A proposed editor workflow for review; saves nothing."""
        return workflow_drafts.draft(store(), providers(), body)

    return routes
