"""Owner schedule definitions and durable run admission; no browser wiring."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from . import jobs
from .agents import StrictModel


class RunRequest(StrictModel):
    request_id: str = Field(min_length=16, max_length=128)


def router(store, authorize):
    routes = APIRouter(prefix="/jobs", dependencies=[Depends(authorize)])

    def call(function, *args):
        try:
            return function(store(), *args)
        except jobs.JobError as exc:
            raise HTTPException(409, str(exc)) from exc

    @routes.get("")
    def listing():
        return jobs.list_jobs(store())

    @routes.post("", status_code=201)
    def create(body: jobs.Definition):
        return call(jobs.create, body)

    @routes.post("/{identity}/update")
    def update(identity: str, body: jobs.Update):
        return call(jobs.update, identity, body)

    @routes.get("/{identity}/runs")
    def runs(identity: str):
        return call(jobs.runs, identity)

    @routes.post("/{identity}/run", status_code=202)
    def run(identity: str, body: RunRequest):
        return call(jobs.run_now, identity, body.request_id)

    return routes
