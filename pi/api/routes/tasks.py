"""Owner task metadata routes. Creating or updating a task never dispatches work."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pi.schedule import tasks


def router(app_state, require_key):
    routes = APIRouter()

    @routes.get("/tasks", dependencies=[Depends(require_key)])
    def list_tasks(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=128),
        session_id: str | None = Query(default=None, max_length=128),
    ):
        return tasks.list_tasks(app_state().store, limit, cursor, session_id)

    @routes.post("/tasks", dependencies=[Depends(require_key)])
    def create_task(body: tasks.CreateTask):
        return tasks.create(app_state().store, body)

    @routes.get("/tasks/requests/{request_id}", dependencies=[Depends(require_key)])
    def task_by_request(request_id: str):
        return tasks.by_request(app_state().store, request_id)

    @routes.get("/tasks/{task_id}", dependencies=[Depends(require_key)])
    def get_task(task_id: str):
        return tasks.get(app_state().store, task_id)

    @routes.post("/tasks/{task_id}/update", dependencies=[Depends(require_key)])
    def update_task(task_id: str, body: tasks.UpdateTask):
        return tasks.update(app_state().store, task_id, body)

    @routes.post("/tasks/{task_id}/transition", dependencies=[Depends(require_key)])
    def transition_task(task_id: str, body: tasks.TransitionTask):
        return tasks.transition(app_state().store, task_id, body)

    @routes.post("/tasks/{task_id}/archive", dependencies=[Depends(require_key)])
    def archive_task(task_id: str, body: tasks.ArchiveTask):
        return tasks.archive(app_state().store, task_id, body)

    return routes
