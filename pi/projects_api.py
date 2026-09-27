"""Durable project endpoints; browser exposure is controlled by the exact owner allowlist."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from . import projects


def router(store, authorize, resolve=None):
    routes = APIRouter(prefix="/projects", dependencies=[Depends(authorize)])

    def run(fn, *args, **kwargs):
        try:
            return fn(store(), *args, **kwargs)
        except projects.ProjectError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    ProjectPath = Annotated[str, Path(pattern=r"^project_[0-9a-f]{32}$")]

    @routes.get("", response_model=projects.ProjectCollection)
    def listing(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, pattern=r"^project_[0-9a-f]{32}$"),
    ):
        rows = run(projects.list_projects, resolve, limit + 1, cursor)
        return projects.ProjectCollection(
            results=rows[:limit], nextCursor=rows[limit - 1]["id"] if len(rows) > limit else None
        )

    @routes.post("", response_model=projects.ProjectView)
    def create(body: projects.Fields):
        return run(projects.create, body)

    @routes.get("/{identity}", response_model=projects.ProjectView)
    def get(identity: ProjectPath):
        return run(projects.get, identity, resolve)

    @routes.post("/{identity}/update", response_model=projects.ProjectView)
    def update(identity: ProjectPath, body: projects.Update):
        return run(projects.mutate, identity, body, "update", resolve)

    @routes.post("/{identity}/archive", response_model=projects.ProjectView)
    def archive(identity: ProjectPath, body: projects.Archive):
        return run(projects.mutate, identity, body, "archive", resolve)

    @routes.post("/{identity}/remove")
    def remove(identity: ProjectPath, body: projects.Revision):
        run(projects.mutate, identity, body, "remove", resolve)
        return {"status": "removed"}

    @routes.post("/{identity}/link", response_model=projects.ProjectView)
    def link(identity: ProjectPath, body: projects.Link):
        return run(projects.mutate, identity, body, "link", resolve)

    @routes.post("/{identity}/unlink", response_model=projects.ProjectView)
    def unlink(identity: ProjectPath, body: projects.Link):
        return run(projects.mutate, identity, body, "unlink", resolve)

    @routes.get("/{identity}/search")
    def search(identity: ProjectPath, query: str = Query(default="", max_length=500)):
        return run(projects.search, identity, query, resolve)

    @routes.post("/{identity}/context")
    def context(identity: ProjectPath, body: projects.Privacy):
        return run(projects.context, identity, body, resolve)

    return routes
