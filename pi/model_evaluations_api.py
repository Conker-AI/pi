"""Explicit admin evaluation requests; GET never runs a provider."""

from fastapi import APIRouter, Depends, HTTPException, Query

from . import agents
from . import model_evaluations as evaluations


def router(store, authorize, providers):
    routes = APIRouter(prefix="/model-evaluations", dependencies=[Depends(authorize)])

    def call(fn, *args):
        try:
            return fn(store(), *args)
        except evaluations.EvaluationError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.get("/cases")
    def cases():
        return call(evaluations.list_cases)

    @routes.post("/cases")
    def create(body: evaluations.Case):
        return call(evaluations.save_case, body)

    @routes.get("/cases/{identity}")
    def get(identity: str):
        return call(evaluations.get_case, identity)

    @routes.post("/cases/{identity}/update")
    def update(identity: str, body: evaluations.Update):
        return call(evaluations.save_case, body.definition, identity, body.expected_revision)

    @routes.post("/cases/{identity}/archive")
    def archive(identity: str, body: agents.ArchiveAgent):
        return call(evaluations.archive_case, identity, body)

    @routes.post("/cases/{identity}/runs")
    def evaluate(identity: str, body: evaluations.Run):
        return call(evaluations.evaluate, identity, body, providers())

    @routes.get("/runs")
    def runs(
        case_id: str | None = Query(None, max_length=200), limit: int = Query(50, ge=1, le=200)
    ):
        return call(evaluations.list_runs, case_id, limit)

    @routes.get("/runs/{request_id}")
    def run(request_id: str):
        return call(evaluations.get_run, request_id)

    return routes
