"""Owner-only character packages, bounded before JSON parsing."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import agents, characters


def router(store, authorize):
    routes = APIRouter(prefix="/characters", dependencies=[Depends(authorize)])

    def execute(fn, *args):
        try:
            result = fn(store(), *args)
            return JSONResponse(
                result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            )
        except (characters.CharacterError, agents.AgentError) as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid character package.") from exc

    async def body(request, model):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > characters.MAX_PACKAGE_BYTES:
                raise HTTPException(413, "Character request exceeds 32 MiB.")
            raw.extend(chunk)
        try:
            return model.model_validate(json.loads(raw))
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid character request.") from exc

    @routes.get("/{agent_id}")
    def get(agent_id: str, revision: int | None = Query(default=None, ge=1)):
        return execute(characters.get, agent_id, revision)

    @routes.put("/{agent_id}")
    async def save(agent_id: str, request: Request):
        return execute(characters.save, agent_id, await body(request, characters.Save))

    @routes.get("/{agent_id}/history")
    def history(agent_id: str):
        return execute(characters.history, agent_id)

    @routes.get("/{agent_id}/export")
    def export(agent_id: str, revision: int | None = Query(default=None, ge=1)):
        return execute(characters.export, agent_id, revision)

    @routes.post("/{agent_id}/import")
    async def import_draft(agent_id: str, request: Request):
        return execute(characters.import_draft, agent_id, await body(request, characters.Import))

    @routes.post("/{agent_id}/restore")
    async def restore(agent_id: str, request: Request):
        return execute(characters.restore, agent_id, await body(request, characters.Restore))

    return routes
