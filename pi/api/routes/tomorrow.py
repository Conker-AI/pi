"""Owner read of what Conker will do on its own soon: jobs due, held jobs, approvals running out."""

import time

from fastapi import APIRouter, Depends, Query

from pi.agents import tomorrow as forecast
from pi.core.models import described


def router(store, authorize):
    routes = APIRouter(prefix="/insights", dependencies=[Depends(authorize)])

    @routes.get("/tomorrow", responses=described(forecast.Tomorrow))
    def tomorrow(hours: int = Query(default=24, ge=1, le=168)):
        """What starts on its own in the next `hours` hours; nothing is run or changed."""
        return forecast.tomorrow(store(), time.time(), hours)

    return routes
