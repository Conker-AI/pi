"""Owner-only character packages, bounded before JSON parsing."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from pi.agents import characters


def router(store, authorize):
    routes = APIRouter(prefix="/characters", dependencies=[Depends(authorize)])

    def execute(fn, *args):
        try:
            result = fn(store(), *args)
            return JSONResponse(
                result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            )
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid character package.") from exc

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > characters.MAX_REQUEST_BYTES:
                raise HTTPException(413, "Character request exceeds 66 MiB.")
            raw.extend(chunk)
        return bytes(raw)

    def parse(raw, model):
        try:
            return model.model_validate(json.loads(raw))
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid character request.") from exc

    async def write(fn, agent_id, request, model):
        raw = await body(request)
        return await run_in_threadpool(lambda: execute(fn, agent_id, parse(raw, model)))

    @routes.get("/{agent_id}")
    def get(agent_id: str, revision: int | None = Query(default=None, ge=1)):
        return execute(characters.get, agent_id, revision)

    @routes.put("/{agent_id}")
    async def save(agent_id: str, request: Request):
        return await write(characters.save, agent_id, request, characters.Save)

    @routes.post("/{agent_id}/save")
    async def browser_save(agent_id: str, request: Request):
        return await write(characters.save, agent_id, request, characters.Save)

    @routes.get("/{agent_id}/history")
    def history(agent_id: str):
        return execute(
            lambda current, selected: {"results": characters.history(current, selected)}, agent_id
        )

    @routes.get("/{agent_id}/export")
    def export(agent_id: str, revision: int | None = Query(default=None, ge=1)):
        return execute(characters.export, agent_id, revision)

    @routes.post("/{agent_id}/import")
    async def import_draft(agent_id: str, request: Request):
        return await write(characters.import_draft, agent_id, request, characters.Import)

    @routes.post("/{agent_id}/restore")
    async def restore(agent_id: str, request: Request):
        return await write(characters.restore, agent_id, request, characters.Restore)

    return routes
