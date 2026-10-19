"""Owner proposal review: list, decide, and run or inspect proposal passes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pi.schedule import proposals


def router(app_state, require_key, require_owner):
    routes = APIRouter()

    @routes.get("/proposals", dependencies=[Depends(require_key)])
    def list_proposals(state: str = Query("open"), limit: int = Query(50, ge=1, le=100)):
        """Things Conker noticed and offers to take on. Listing grants no authority."""
        return proposals.list_proposals(app_state().store, state, limit)

    @routes.post("/proposals/{proposal_id}/decision", dependencies=[Depends(require_key)])
    def decide_proposal(proposal_id: str, body: proposals.Decision):
        """Accept, decline, or never show again. Accepting records a decision; nothing runs."""
        return proposals.decide(app_state().store, proposal_id, body)

    @routes.get("/proposals/passes", dependencies=[Depends(require_key)])
    def list_proposal_passes(limit: int = Query(20, ge=1, le=100)):
        return proposals.passes(app_state().store, limit)

    @routes.post("/proposals/passes", dependencies=[Depends(require_owner)])
    def run_proposal_pass():
        """Owner-requested pass. Budget, quiet hours and the role setting still apply."""
        enabled, complete = proposals.model_call(app_state().store, app_state().router)
        return proposals.run_pass(app_state().store, complete, role_enabled=enabled)

    return routes
