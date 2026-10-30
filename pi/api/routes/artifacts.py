"""Bounded artifact routes for exact owner-browser and recovery-admin use."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from pi.content import artifact_models, artifacts, office_exports


def attachment(content: bytes, mime: str, filename: str, readable: str | None = None):
    """A file download that is never shown inline, cached or sniffed into something else."""
    return Response(
        content,
        media_type=mime,
        headers={
            "Content-Disposition": office_exports.disposition(filename, readable or filename),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def router(store, authorize, resolve=None):
    routes = APIRouter(prefix="/artifacts", dependencies=[Depends(authorize)])

    def run(fn, *args, **kwargs):
        try:
            return fn(store(), *args, resolve=resolve, **kwargs)
        except ValueError as exc:
            raise HTTPException(422, "Invalid artifact content.") from exc

    ArtifactPath = Annotated[str, Path(pattern=r"^artifact_[0-9a-f]{32}$")]

    @routes.get("", response_model=artifact_models.ArtifactCollection)
    def listing(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, pattern=r"^artifact_[0-9a-f]{32}$"),
    ):
        rows = run(artifacts.list_artifacts, limit=limit + 1, cursor=cursor)
        return artifact_models.ArtifactCollection(
            results=rows[:limit], nextCursor=rows[limit - 1]["id"] if len(rows) > limit else None
        )

    @routes.post("", response_model=artifact_models.ArtifactView)
    def create(body: artifact_models.Create):
        return run(artifacts.create, body)

    @routes.post("/from-message", response_model=artifact_models.ArtifactView)
    def copy(body: artifact_models.FromMessage):
        return run(artifacts.create, body)

    @routes.get("/{identity}", response_model=artifact_models.ArtifactView)
    def get(identity: ArtifactPath):
        return run(artifacts.get, identity)

    @routes.post("/{identity}/versions", response_model=artifact_models.ArtifactView)
    def append(identity: ArtifactPath, body: artifact_models.Append):
        return run(artifacts.mutate, identity, body)

    @routes.post("/{identity}/restore", response_model=artifact_models.ArtifactView)
    def restore(identity: ArtifactPath, body: artifact_models.Restore):
        return run(artifacts.mutate, identity, body)

    @routes.post("/{identity}/archive", response_model=artifact_models.ArtifactView)
    def archive(identity: ArtifactPath, body: artifact_models.Archive):
        return run(artifacts.mutate, identity, body)

    @routes.get("/{identity}/export", response_model=artifact_models.NativeExport)
    def export(identity: ArtifactPath, version: int | None = Query(default=None, ge=1, le=100)):
        return run(artifacts.export, identity, version)

    @routes.get("/{identity}/download")
    def download(
        identity: ArtifactPath,
        version: int | None = Query(default=None, ge=1, le=100),
        format: Literal["native", "docx", "pdf", "pptx", "xlsx"] = "native",
    ):
        result = run(artifacts.export, identity, version, format=format)
        if format == "native":
            return attachment(result["text"].encode("utf-8"), result["mime"], result["filename"])
        return attachment(result["content"], result["mime"], result["filename"], result["readable"])

    return routes


def answers(store, authorize, resolve=None):
    """Download one completed chat answer as a document, deck or spreadsheet."""
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.get("/sessions/{session_id}/messages/{message_id}/download")
    def download(
        session_id: Annotated[str, Path(pattern=r"^ses_[0-9a-f]{16}$")],
        message_id: Annotated[str, Path(pattern=r"^msg_[0-9a-f]{16}$")],
        format: Literal["docx", "pdf", "pptx", "xlsx"],
    ):
        result = artifacts.export_answer(store(), session_id, message_id, resolve, format=format)
        return attachment(result["content"], result["mime"], result["filename"], result["readable"])

    return routes
