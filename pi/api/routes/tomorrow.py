"""Owner read of what Conker will do on its own soon: jobs due, held jobs, approvals running out,
and each start with whether it would run and why not; on request, the calendar's events too."""

import time

from fastapi import APIRouter, Depends, Query, Request

from pi.agents import tomorrow as forecast
from pi.context import calendar_events
from pi.core.models import described


def router(store, authorize):
    routes = APIRouter(prefix="/insights", dependencies=[Depends(authorize)])

    @routes.get("/tomorrow", responses=described(forecast.Tomorrow))
    def tomorrow(
        request: Request,
        hours: int = Query(default=24, ge=1, le=168),
        events: bool = Query(default=False),
    ):
        """What starts on its own in the next `hours` hours; nothing is run or changed. With
        `events=true`, also the calendar's events in that window (one read-only ToolGate call)."""
        state = request.app.state  # the workers Pi started (pi.api.startup), None when off
        now = time.time()
        body = forecast.tomorrow(
            store(),
            now,
            hours,
            scheduler=getattr(state, "scheduler", None) is not None,
            proposal_worker=getattr(state, "proposal_worker", None),
        )
        if events:
            client = getattr(state, "toolgate", None)
            body["events"] = calendar_events.upcoming(store(), client, now, hours, body["timeZone"])
        return body

    return routes
