"""Owner attachment transport. All bytes are inert and bounded before persistence."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from pi.content import attachment_files, attachments


def router(store, authorize, resolve=None):
    routes = APIRouter(
        prefix="/sessions/{session_id}/attachments", dependencies=[Depends(authorize)]
    )

    def run(fn, *args):
        try:
            return fn(store(), *args, resolve=resolve)
        except ValueError as exc:
            raise HTTPException(422, "Invalid attachment metadata.") from exc

    @routes.post("")
    async def upload(
        session_id: str,
        request: Request,
        name: str = Query(min_length=1, max_length=255),
        type: str = Query(default="", max_length=255),
        lastModified: float = Query(default=0, ge=0),
    ):
        try:
            metadata = attachments.Metadata(name=name, type=type, lastModified=lastModified)
        except ValueError as exc:
            raise HTTPException(422, "Invalid attachment metadata.") from exc
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError as exc:
                raise HTTPException(400, "Invalid content length.") from exc
            if size < 0:
                raise HTTPException(400, "Invalid content length.")

        # Chunks go straight to disk, off the event loop; no size cap.
        incoming = attachment_files.Incoming(store())
        try:
            async for chunk in request.stream():
                await run_in_threadpool(incoming.write, chunk)
            incoming_path, size, sha256 = await run_in_threadpool(incoming.close)
            if declared is not None and size != int(declared):
                incoming.discard()
                raise HTTPException(400, "Attachment content length mismatch.")
            return await run_in_threadpool(
                run, attachments.upload_file, session_id, metadata, incoming_path, size, sha256
            )
        except Exception:
            incoming.discard()
            raise

    @routes.get("")
    def listing(session_id: str):
        return run(attachments.listing, session_id)

    @routes.get("/{identity}")
    def get(session_id: str, identity: str):
        return run(attachments.get, session_id, identity)

    @routes.delete("/{identity}")
    def remove(session_id: str, identity: str):
        return run(attachments.remove, session_id, identity)

    @routes.get("/{identity}/download")
    def download(session_id: str, identity: str):
        view, content = run(attachments.download_file, session_id, identity)
        headers = {
            "Content-Disposition": "attachment; filename=\"attachment.bin\"; filename*=UTF-8''"
            + quote(view["name"], safe=""),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        }
        if isinstance(content, bytes):
            return Response(content, media_type="application/octet-stream", headers=headers)
        return FileResponse(content, media_type="application/octet-stream", headers=headers)

    @routes.get("/{identity}/text")
    def extract(session_id: str, identity: str):
        return run(attachments.extract, session_id, identity)

    @routes.get("/{identity}/passages/{index}")
    def passage(session_id: str, identity: str, index: int):
        return JSONResponse(
            run(attachments.passage, session_id, identity, index),
            headers={"Cache-Control": "no-store"},
        )

    return routes
