"""Pi's self-check for the owner: the last state of each check and the alerts still open.

Reading changes nothing; dismissing hides one alert (a signed-in browser session may do it).
"""

from fastapi import APIRouter, Depends, Path

from pi.owner import self_check


def router(store, authorize):
    routes = APIRouter(prefix="/self-check", dependencies=[Depends(authorize)])

    @routes.get("")
    def overview():
        """Each check's last status and since when, and the open alerts."""
        return self_check.overview(store())

    @routes.post("/alerts/{alert_id}/dismiss")
    def dismiss(alert_id: str = Path(pattern=r"^sc_[0-9a-f]{32}$")):
        """Hide an alert until its check recovers and breaks again."""
        return self_check.dismiss(store(), alert_id)

    return routes
