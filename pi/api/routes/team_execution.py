"""Owner-only explicit team dispatch; no worker or automatic handoff evaluation."""

from fastapi import APIRouter, Depends, Response

from pi.agents import team_background, team_models
from pi.agents import team_execution as teams


def create_router(store_provider, loop_provider, authorize):
    router = APIRouter(prefix="/team-runs", dependencies=[Depends(authorize)])

    @router.post("/from-team/{team_id}")
    def start(team_id: str, body: team_models.Start):
        return teams.start(store_provider(), team_id, body)

    @router.get("/{identity}")
    def get(identity: str):
        return teams.get(store_provider(), identity)

    @router.post(
        "/{identity}/steps",
        responses={202: {"description": "Started in the background; read the run until done."}},
    )
    def execute(identity: str, body: team_models.Step, response: Response):
        if body.background:
            result = team_background.execute_in_background(
                store_provider(), loop_provider(), identity, body
            )
            # 202 while the role is still working; a replay of a finished step reads 200.
            response.status_code = 202 if result["state"] == "running" else 200
            return result
        return teams.execute(store_provider(), loop_provider(), identity, body)

    @router.post("/{identity}/reconcile")
    def reconcile(identity: str):
        return teams.reconcile(store_provider(), identity)

    @router.post("/{identity}/finish")
    def finish(identity: str, body: team_models.Finish):
        return teams.finish(store_provider(), identity, body)

    return router
