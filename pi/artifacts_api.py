"""Independent owner-only artifact routes; no gateway allowlist changes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from . import artifacts


def router(store, authorize, resolve=None):
    routes = APIRouter(prefix="/artifacts", dependencies=[Depends(authorize)])

    def run(fn, *args, **kwargs):
        try:
            return fn(store(), *args, resolve=resolve, **kwargs)
        except artifacts.ArtifactError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid artifact content.") from exc

    @routes.get("")
    def listing():
        return run(artifacts.list_artifacts)

    @routes.post("")
    def create(body: artifacts.Create):
        return run(artifacts.create, body)

    @routes.post("/from-message")
    def copy(body: artifacts.FromMessage):
        return run(artifacts.create, body)

    @routes.get("/{identity}")
    def get(identity: str):
        return run(artifacts.get, identity)

    @routes.post("/{identity}/versions")
    def append(identity: str, body: artifacts.Append):
        return run(artifacts.mutate, identity, body)

    @routes.post("/{identity}/restore")
    def restore(identity: str, body: artifacts.Restore):
        return run(artifacts.mutate, identity, body)

    @routes.post("/{identity}/archive")
    def archive(identity: str, body: artifacts.Archive):
        return run(artifacts.mutate, identity, body)

    @routes.get("/{identity}/export")
    def export(identity: str, version: int | None = Query(default=None, ge=1)):
        return run(artifacts.export, identity, version)

    return routes
