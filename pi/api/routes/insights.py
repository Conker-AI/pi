"""Owner read of how much Conker needed him: counts only, never his words."""

import time

from fastapi import APIRouter, Depends, Query

from pi.agents import insights
from pi.core.models import described


def router(store, authorize):
    routes = APIRouter(prefix="/insights", dependencies=[Depends(authorize)])

    @routes.get("/quiet", responses=described(insights.QuietInsights))
    def quiet(days: int = Query(default=7, ge=1, le=90)):
        """The last `days` days against the `days` before: what asked for the owner, what did not."""
        return insights.quiet(store(), time.time(), days)

    return routes
