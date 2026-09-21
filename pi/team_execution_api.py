"""Owner-only explicit team dispatch; no worker or automatic handoff evaluation."""

from fastapi import APIRouter, Depends

from . import team_execution as teams


def create_router(store_provider, loop_provider, authorize):
    router = APIRouter(prefix="/team-runs", dependencies=[Depends(authorize)])

    @router.post("/from-team/{team_id}")
    def start(team_id: str, body: teams.Start):
        return teams.start(store_provider(), team_id, body)

    @router.get("/{identity}")
    def get(identity: str):
        return teams.get(store_provider(), identity)

    @router.post("/{identity}/steps")
    def execute(identity: str, body: teams.Step):
        return teams.execute(store_provider(), loop_provider(), identity, body)

    @router.post("/{identity}/reconcile")
    def reconcile(identity: str):
        return teams.reconcile(store_provider(), identity)

    @router.post("/{identity}/finish")
    def finish(identity: str, body: teams.Finish):
        return teams.finish(store_provider(), identity, body)

    return router
