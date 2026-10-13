"""Read projections of runs and content-free events."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from . import activity


def router(app_state, require_key):
    routes = APIRouter()

    @routes.get("/runs", dependencies=[Depends(require_key)])
    def list_runs(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=128),
        session_id: str | None = Query(default=None, max_length=128),
        task_id: str | None = Query(default=None, max_length=128),
    ):
        return activity.list_runs(app_state().store, limit, cursor, session_id, task_id)

    @routes.get("/runs/{run_id}", dependencies=[Depends(require_key)])
    def get_run(run_id: str):
        return activity.get_run(app_state().store, run_id)

    @routes.get("/events", dependencies=[Depends(require_key)])
    def list_events(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=128),
        session_id: str | None = Query(default=None, max_length=128),
        task_id: str | None = Query(default=None, max_length=128),
        run_id: str | None = Query(default=None, max_length=128),
    ):
        return activity.list_events(app_state().store, limit, cursor, session_id, task_id, run_id)

    return routes
