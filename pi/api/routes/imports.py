"""Owner routes for importing ChatGPT and Claude exports: preview, import, then read the archive.

The body of a preview or an import is the export's `conversations.json` itself, read in bounded
chunks; a preview saves nothing. An import names the previewed file by its SHA-256 digest, so the
owner's yes is for exactly the file he saw. Nothing here runs a model, a tool or a turn.

The router's lifespan starts the sender that offers imported words to MemoryGate's import
listener (`pi.memory.import_offers`), after Pi's own startup and stopped before it closes.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from starlette.concurrency import run_in_threadpool

from pi.memory import import_formats, import_offers, imports

Source = Literal["chatgpt", "claude"]


@asynccontextmanager
async def lifespan(app):
    offers = import_offers.Offers(app.state.store, import_offers.from_environment(os.environ))
    app.state.import_offers = offers
    offers.start()
    try:
        yield
    finally:
        offers.close()


def _too_large():
    limit = import_formats.MAX_BYTES // (1024 * 1024)
    return imports.ImportRefused("too_large", f"The file is larger than {limit} MB.", 413)


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise HTTPException(400, "Invalid content length.") from None
        if size < 0:
            raise HTTPException(400, "Invalid content length.")
        if size > import_formats.MAX_BYTES:
            raise _too_large()
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > import_formats.MAX_BYTES:
            raise _too_large()
    return bytes(raw)


def router(store, offers, authorize):
    api = APIRouter(prefix="/imports", dependencies=[Depends(authorize)], lifespan=lifespan)

    def configured():
        sender = offers()
        return sender is not None and sender.client is not None

    @api.post("/{source}/preview")
    async def preview(source: Source, request: Request):
        raw = await _body(request)
        return await run_in_threadpool(
            lambda: imports.preview(store(), source, raw, memory_configured=configured())
        )

    @api.post("/{source}/{digest}")
    async def save(source: Source, request: Request, digest: str = Path(pattern=r"^[0-9a-f]{64}$")):
        raw = await _body(request)
        result = await run_in_threadpool(
            lambda: imports.save(store(), source, raw, digest, memory_configured=configured())
        )
        sender = offers()
        if sender is not None:
            sender.wake()
        return result

    @api.get("")
    def listing():
        return {
            "schemaVersion": 1,
            "imports": imports.batches(store()),
            "memory": import_offers.status(store(), offers()),
        }

    @api.get("/chats")
    def chats(
        source: Source | None = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1_000_000),
    ):
        return imports.chats(store(), source=source, limit=limit, offset=offset)

    @api.get("/chats/{chat_id}")
    def chat(
        chat_id: str = Path(pattern=r"^imp_[0-9a-f]{32}$"),
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
    ):
        return imports.chat(store(), chat_id, after=after, limit=limit)

    return api
