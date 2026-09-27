"""Bounded artifact routes for exact owner-browser and recovery-admin use."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

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

    ArtifactPath = Annotated[str, Path(pattern=r"^artifact_[0-9a-f]{32}$")]

    @routes.get("", response_model=artifacts.ArtifactCollection)
    def listing(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, pattern=r"^artifact_[0-9a-f]{32}$"),
    ):
        rows = run(artifacts.list_artifacts, limit=limit + 1, cursor=cursor)
        return artifacts.ArtifactCollection(
            results=rows[:limit], nextCursor=rows[limit - 1]["id"] if len(rows) > limit else None
        )

    @routes.post("", response_model=artifacts.ArtifactView)
    def create(body: artifacts.Create):
        return run(artifacts.create, body)

    @routes.post("/from-message", response_model=artifacts.ArtifactView)
    def copy(body: artifacts.FromMessage):
        return run(artifacts.create, body)

    @routes.get("/{identity}", response_model=artifacts.ArtifactView)
    def get(identity: ArtifactPath):
        return run(artifacts.get, identity)

    @routes.post("/{identity}/versions", response_model=artifacts.ArtifactView)
    def append(identity: ArtifactPath, body: artifacts.Append):
        return run(artifacts.mutate, identity, body)

    @routes.post("/{identity}/restore", response_model=artifacts.ArtifactView)
    def restore(identity: ArtifactPath, body: artifacts.Restore):
        return run(artifacts.mutate, identity, body)

    @routes.post("/{identity}/archive", response_model=artifacts.ArtifactView)
    def archive(identity: ArtifactPath, body: artifacts.Archive):
        return run(artifacts.mutate, identity, body)

    @routes.get("/{identity}/export", response_model=artifacts.NativeExport)
    def export(identity: ArtifactPath, version: int | None = Query(default=None, ge=1, le=100)):
        return run(artifacts.export, identity, version)

    @routes.get("/{identity}/download")
    def download(
        identity: ArtifactPath,
        version: int | None = Query(default=None, ge=1, le=100),
        format: Literal["native", "docx", "xlsx"] = "native",
    ):
        result = run(artifacts.export, identity, version, format=format)
        content = result["text"].encode("utf-8") if format == "native" else result["content"]
        return Response(
            content,
            media_type=result["mime"],
            headers={
                "Content-Disposition": f'attachment; filename="{result["filename"]}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return routes
