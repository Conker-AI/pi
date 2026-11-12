"""The health watch for the owner: each check's latest outcome, and dismissing a card.

Recovery credential only for now; the owner-control browser path is not open to it yet
(see browser_contract), so the dashboard reads these cards once it is.
"""

from fastapi import APIRouter, Depends, Path

from pi.owner import health_watch


def router(store, authorize):
    routes = APIRouter(prefix="/health-watch", dependencies=[Depends(authorize)])

    @routes.get("")
    def overview():
        """Each check's latest outcome and the cards still open."""
        return health_watch.overview(store())

    @routes.post("/alerts/{alert_id}/dismiss")
    def dismiss(alert_id: str = Path(pattern=r"^health_[0-9a-f]{32}$")):
        """Hide a card until its check works again."""
        return health_watch.dismiss(store(), alert_id)

    return routes
