"""Owner-only project endpoints; no browser runtime allowlist changes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from . import projects


def router(store, authorize, resolve=None):
    routes = APIRouter(prefix="/projects", dependencies=[Depends(authorize)])

    def run(fn, *args, **kwargs):
        try:
            return fn(store(), *args, **kwargs)
        except projects.ProjectError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @routes.get("")
    def listing():
        return run(projects.list_projects, resolve)

    @routes.post("")
    def create(body: projects.Fields):
        return run(projects.create, body)

    @routes.get("/{identity}")
    def get(identity: str):
        return run(projects.get, identity, resolve)

    @routes.post("/{identity}/update")
    def update(identity: str, body: projects.Update):
        return run(projects.mutate, identity, body, "update", resolve)

    @routes.post("/{identity}/archive")
    def archive(identity: str, body: projects.Archive):
        return run(projects.mutate, identity, body, "archive", resolve)

    @routes.post("/{identity}/remove")
    def remove(identity: str, body: projects.Revision):
        run(projects.mutate, identity, body, "remove", resolve)
        return {"status": "removed"}

    @routes.post("/{identity}/link")
    def link(identity: str, body: projects.Link):
        return run(projects.mutate, identity, body, "link", resolve)

    @routes.post("/{identity}/unlink")
    def unlink(identity: str, body: projects.Link):
        return run(projects.mutate, identity, body, "unlink", resolve)

    @routes.get("/{identity}/search")
    def search(identity: str, query: str = Query(default="", max_length=500)):
        return run(projects.search, identity, query, resolve)

    @routes.post("/{identity}/context")
    def context(identity: str, body: projects.Privacy):
        return run(projects.context, identity, body, resolve)

    return routes
