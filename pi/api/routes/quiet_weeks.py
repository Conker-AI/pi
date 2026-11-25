"""Owner read of how much Conker reached for him, week by week: counts only, never his words."""

import time

from fastapi import APIRouter, Depends, Query

from pi.agents import quiet_weeks
from pi.core.models import described


def router(store, authorize):
    routes = APIRouter(prefix="/insights", dependencies=[Depends(authorize)])

    @routes.get("/quiet/weeks", responses=described(quiet_weeks.QuietWeeks))
    def weeks(weeks: int = Query(default=8, ge=1, le=26)):
        """The last `weeks` ISO weeks in the owner's zone: what Conker raised, what became of it."""
        return quiet_weeks.quiet_weeks(store(), time.time(), weeks)

    return routes
