"""Owner attachment transport. All bytes are inert and bounded before persistence."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import attachments


def router(store, authorize, resolve=None):
    routes = APIRouter(
        prefix="/sessions/{session_id}/attachments", dependencies=[Depends(authorize)]
    )

    def run(fn, *args):
        try:
            return fn(store(), *args, resolve=resolve)
        except attachments.AttachmentError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
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
            if size < 0 or size > attachments.MAX_BYTES:
                raise HTTPException(413, "Attachment must be at most 10 MiB.")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > attachments.MAX_BYTES:
                raise HTTPException(413, "Attachment must be at most 10 MiB.")
            raw.extend(chunk)
        if declared is not None and len(raw) != size:
            raise HTTPException(400, "Attachment content length mismatch.")
        return run(attachments.upload, session_id, metadata, bytes(raw))

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
        view, raw = run(attachments.download, session_id, identity)
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename=\"attachment.bin\"; filename*=UTF-8''"
                + quote(view["name"], safe=""),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @routes.get("/{identity}/text")
    def extract(session_id: str, identity: str):
        return run(attachments.extract, session_id, identity)

    return routes
