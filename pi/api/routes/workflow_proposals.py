"""Workflow drafts made from chat messages, as the Inbox lists them."""

from fastapi import APIRouter, Depends, Path

from pi.agents import workflow_proposals


def router(store, authorize):
    routes = APIRouter(prefix="/workflow-proposals", dependencies=[Depends(authorize)])

    @routes.post("")
    def record(body: workflow_proposals.Record):
        """Link one of the owner's messages to the editor draft made from it."""
        return workflow_proposals.record(store(), body)

    @routes.get("")
    def listing():
        """Drafts made from chat messages that still wait for the owner."""
        return {"items": workflow_proposals.open_items(store())}

    @routes.post("/published")
    def published(body: workflow_proposals.Published):
        """The owner published this draft; its Inbox items are done."""
        return workflow_proposals.published(store(), body)

    @routes.post("/{proposal_id}/dismiss")
    def dismiss(proposal_id: str = Path(pattern=r"^wfp_[0-9a-f]{32}$")):
        """Remove a draft from the Inbox without publishing it."""
        return workflow_proposals.dismiss(store(), proposal_id)

    return routes
