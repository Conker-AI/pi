"""Owner read projection over the server-bound MemoryGate namespace."""
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

Kind = Literal["memory", "entity", "evidence", "analysis", "episode", "observation", "pattern", "transcript"]


def router(memory, authorize):
    routes = APIRouter(prefix="/memory/objects", dependencies=[Depends(authorize)])

    def inspect(operation, payload):
        client = memory().client
        if client is None:
            raise HTTPException(503, "MemoryGate is not configured.")
        try:
            return client.inspect(operation, {"scope": "all", **payload})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {404, 422}:
                raise HTTPException(exc.response.status_code, "Memory record or field is unavailable.") from None
            raise HTTPException(503, "MemoryGate inspection is unavailable.") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(503, "MemoryGate inspection is unavailable.") from None

    @routes.get("")
    def library(search: str = Query(default="", max_length=200),
                object_type: Kind | None = None, after: str | None = Query(default=None, max_length=240),
                limit: int = Query(default=25, ge=1, le=50)):
        return inspect("library", {"search": search, "object_type": object_type, "after": after, "limit": limit})

    @routes.get("/{kind}/{identity}")
    def detail(kind: Kind, identity: str, operation: Literal["connections", "content"] = "connections",
               after: str | None = Query(default=None, max_length=240),
               relationship: str | None = Query(default=None, max_length=120),
               field: str = Query(default="summary", max_length=60),
               offset: int = Query(default=0, ge=0, le=100000000),
               characters: int = Query(default=4000, ge=1, le=16000)):
        if not 1 <= len(identity) <= 200:
            raise HTTPException(422, "Invalid memory identity.")
        return inspect("explore", {"object_type": kind, "object_id": identity, "operation": operation,
                                  "after": after, "relationship": relationship, "field": field,
                                  "offset": offset, "characters": characters})

    return routes
