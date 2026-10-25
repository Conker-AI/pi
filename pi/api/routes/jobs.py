"""Bounded owner schedule control with redacted run evidence."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import Field

from pi.core.models import Strict
from pi.schedule import agent_checks, job_models, jobs


class RunRequest(Strict):
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


def router(store, authorize, executor=None):
    routes = APIRouter(prefix="/jobs", dependencies=[Depends(authorize)])

    def call(function, *args, **kwargs):
        return function(store(), *args, **kwargs)

    JobPath = Annotated[str, Path(pattern=r"^job_[a-f0-9]{32}$")]
    RunPath = Annotated[str, Path(pattern=r"^scheduled_[a-f0-9]{32}$")]

    @routes.get("", response_model=job_models.JobCollection)
    def listing(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, pattern=r"^job_[a-f0-9]{32}$"),
    ):
        rows = call(jobs.list_jobs, limit=limit + 1, cursor=cursor)
        return job_models.JobCollection(
            results=[jobs.browser_job(row) for row in rows[:limit]],
            nextCursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )

    @routes.post("/checks", status_code=201, response_model=job_models.JobView)
    def create_check(body: job_models.CheckCreate):
        """An AI check: its agent runs on a schedule in its own chat; tools still ask as in chat."""
        return jobs.browser_job(call(agent_checks.create_check, body))

    @routes.get("/checks/notes", response_model=job_models.CheckNoteCollection)
    def check_notes():
        """What AI checks found for the owner, newest first, until dismissed."""
        return job_models.CheckNoteCollection(results=call(agent_checks.notes))

    @routes.post("/checks/notes/{identity}/dismiss")
    def dismiss_check_note(identity: Annotated[str, Path(pattern=agent_checks.NOTE_ID)]):
        """Hide a note; the check's chat keeps the full reply."""
        return call(agent_checks.dismiss, identity)

    @routes.get("/{identity}", response_model=job_models.JobView)
    def get(identity: JobPath):
        return jobs.browser_job(call(jobs.get_job, identity))

    @routes.post("", status_code=201, response_model=job_models.JobView)
    def create(body: job_models.Definition):
        return jobs.browser_job(call(jobs.create, body))

    @routes.post("/{identity}/update", response_model=job_models.JobView)
    def update(identity: JobPath, body: job_models.Update):
        return jobs.browser_job(call(jobs.update, identity, body))

    @routes.post("/{identity}/state", response_model=job_models.JobView)
    def state(identity: JobPath, body: job_models.StateChange):
        return jobs.browser_job(call(jobs.set_enabled, identity, body))

    @routes.get("/{identity}/runs", response_model=job_models.RunCollection)
    def runs(
        identity: JobPath,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, pattern=r"^scheduled_[a-f0-9]{32}$"),
    ):
        rows = call(jobs.runs, identity, limit=limit + 1, cursor=cursor)
        return job_models.RunCollection(
            results=[jobs.browser_run(row) for row in rows[:limit]],
            nextCursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )

    @routes.post("/{identity}/run", status_code=202, response_model=job_models.RunActionView)
    def run(identity: JobPath, body: RunRequest):
        admitted = call(jobs.run_now, identity, body.request_id)
        return jobs.browser_run_action(
            call(jobs.get_run, admitted["id"]), admitted.get("replayed", False)
        )

    def execution_adapter():
        adapter = executor() if executor else None
        if adapter is None:
            raise HTTPException(503, "Scheduled execution adapter is not configured.")
        return adapter

    @routes.post("/runs/{identity}/budget", response_model=job_models.RunActionView)
    def budget(identity: RunPath, body: job_models.BudgetBinding):
        result = call(jobs.bind_budget, identity, body, execution_adapter())
        return jobs.browser_run_action(call(jobs.get_run, identity), result["replayed"])

    @routes.post("/runs/{identity}/provision-budget", response_model=job_models.RunView)
    def provision_budget(identity: RunPath):
        call(jobs.provision_budget, identity, execution_adapter())
        return jobs.browser_run(call(jobs.get_run, identity))

    @routes.post("/runs/{identity}/cancel", response_model=job_models.RunActionView)
    def cancel(identity: RunPath):
        result = call(jobs.cancel_run, identity)
        return jobs.browser_run_action(call(jobs.get_run, identity), result["replayed"])

    @routes.post("/runs/{identity}/resume", response_model=job_models.RunView)
    def resume(identity: RunPath):
        jobs.dispatch_claim(store(), {"id": identity}, execution_adapter(), resume=True)
        return jobs.browser_run(call(jobs.get_run, identity))

    @routes.post("/runs/{identity}/reconcile", response_model=job_models.RunView)
    def reconcile(identity: RunPath):
        call(jobs.reconcile, identity, execution_adapter())
        return jobs.browser_run(call(jobs.get_run, identity))

    return routes
