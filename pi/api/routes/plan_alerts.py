"""Plan usage readings and alerts for the owner; reading changes nothing.

The owner-control credential may read `/plans/usage` only (see browser_contract); dismissing
stays with the recovery credential.
"""

from fastapi import APIRouter, Depends, Path

from pi.owner import plan_alerts


def router(store, authorize):
    routes = APIRouter(prefix="/plans", dependencies=[Depends(authorize)])

    @routes.get("/usage")
    def usage():
        """The latest reading of each plan window and the alerts still open."""
        return plan_alerts.overview(store())

    @routes.post("/alerts/{alert_id}/dismiss")
    def dismiss(alert_id: str = Path(pattern=r"^plan_[0-9a-f]{32}$")):
        """Hide an alert until its window resets."""
        return plan_alerts.dismiss(store(), alert_id)

    return routes
