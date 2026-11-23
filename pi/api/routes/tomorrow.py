"""Owner read of what Conker will do on its own soon: jobs due, held jobs, approvals running out,
and each start with whether it would run and why not."""

import time

from fastapi import APIRouter, Depends, Query, Request

from pi.agents import tomorrow as forecast
from pi.core.models import described


def router(store, authorize):
    routes = APIRouter(prefix="/insights", dependencies=[Depends(authorize)])

    @routes.get("/tomorrow", responses=described(forecast.Tomorrow))
    def tomorrow(request: Request, hours: int = Query(default=24, ge=1, le=168)):
        """What starts on its own in the next `hours` hours; nothing is run or changed."""
        state = request.app.state  # the workers Pi started (pi.api.startup), None when off
        return forecast.tomorrow(
            store(),
            time.time(),
            hours,
            scheduler=getattr(state, "scheduler", None) is not None,
            proposal_worker=getattr(state, "proposal_worker", None),
        )

    return routes
