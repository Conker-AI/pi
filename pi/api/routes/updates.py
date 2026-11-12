"""Conker update offers and results (pi.owner.updates).

The owner-control credential reads them and dismisses one (see browser_contract); only the
recovery credential, which stays on the host, reports what the host found.
"""

from fastapi import APIRouter, Depends, Path

from pi.owner import updates


def router(store, authorize_owner, authorize_admin):
    routes = APIRouter(prefix="/updates")

    @routes.get("", dependencies=[Depends(authorize_owner)])
    def overview():
        """The newest release still offered and every update result not dismissed."""
        return updates.overview(store())

    @routes.post("/offers/{version}/dismiss", dependencies=[Depends(authorize_owner)])
    def dismiss_offer(version: str = Path(pattern=updates.VERSION)):
        """Stop offering this version; a newer release makes a new offer."""
        return updates.dismiss_offer(store(), version)

    @routes.post("/results/{result_id}/dismiss", dependencies=[Depends(authorize_owner)])
    def dismiss_result(result_id: str = Path(pattern=updates.RESULT_ID)):
        """Clear one update result from the Inbox."""
        return updates.dismiss_result(store(), result_id)

    @routes.post("/report", dependencies=[Depends(authorize_admin)])
    def report(body: updates.Report):
        """The host's daily check and finished update receipts. Sending one twice changes nothing."""
        return updates.record(store(), body)

    return routes
