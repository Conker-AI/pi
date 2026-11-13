"""Owner read projection over the server-bound MemoryGate namespace.

Records that come from a hidden chat (forgotten or in the Trash, pi.conversation.visibility) are
left out of the library and of every detail view, by the same set recall uses
(pi.memory.hidden.hidden_objects). MemoryGate's cards carry no chat, so only what that set can
name is left out: the evidence, analysis and memory made from a delivered message, and memories
Conker kept from the chat. Restoring the chat brings them back.
"""

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from pi.core.models import described
from pi.memory.hidden import hidden_objects
from pi.memory.memory_replies import MemoryLibraryPage, MemoryObjectKind

Kind = MemoryObjectKind
UNAVAILABLE = "Memory record or field is unavailable."


def _hidden(service, client) -> set[tuple[str, str]]:
    """(kind, id) of every record from a hidden chat. A test double without a store hides none."""
    store = getattr(service, "store", None)
    if store is None:
        return set()
    with store._connect() as db:
        found = hidden_objects(db, (getattr(client, "agent_id", None),))
    return {(kind, identity) for kind, ids in found.items() for identity in ids}


def _is_hidden(card, gone) -> bool:
    return isinstance(card, dict) and (card.get("type"), card.get("id")) in gone


def _library_view(result, gone):
    objects = result.get("objects")
    if not gone or not isinstance(objects, list):
        return result
    shown = [item for item in objects if not _is_hidden(item, gone)]
    if len(shown) == len(objects):
        return result
    total = result.get("total")
    # MemoryGate counted the whole scope; only this page's hidden records are known to be in it.
    if isinstance(total, int) and not isinstance(total, bool):
        total = max(0, total - (len(objects) - len(shown)))
    return {**result, "objects": shown, "total": total}


def _detail_view(result, gone):
    if not gone:
        return result
    if _is_hidden(result.get("object"), gone):
        raise HTTPException(404, UNAVAILABLE)
    view = dict(result)
    links = result.get("links")
    if isinstance(links, list):
        view["links"] = [
            link
            for link in links
            if not isinstance(link, dict)
            or not (
                (link.get("source_type"), link.get("source_id")) in gone
                or (link.get("target_type"), link.get("target_id")) in gone
            )
        ]
    nodes = result.get("nodes")
    if isinstance(nodes, list):
        view["nodes"] = [node for node in nodes if not _is_hidden(node, gone)]
    return view


def router(memory, authorize):
    routes = APIRouter(prefix="/memory/objects", dependencies=[Depends(authorize)])

    def inspect(operation, payload):
        """MemoryGate's answer and the hidden records to leave out of it."""
        service = memory()
        client = service.client
        if client is None:
            raise HTTPException(503, "MemoryGate is not configured.")
        gone = _hidden(service, client)
        if (payload.get("object_type"), payload.get("object_id")) in gone:
            raise HTTPException(404, UNAVAILABLE)
        try:
            return client.inspect(operation, {"scope": "all", **payload}), gone
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {404, 422}:
                raise HTTPException(exc.response.status_code, UNAVAILABLE) from None
            raise HTTPException(503, "MemoryGate inspection is unavailable.") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(503, "MemoryGate inspection is unavailable.") from None

    @routes.get("", responses=described(MemoryLibraryPage))
    def library(
        search: str = Query(default="", max_length=200),
        object_type: Kind | None = None,
        after: str | None = Query(default=None, max_length=240),
        limit: int = Query(default=25, ge=1, le=50),
    ):
        return _library_view(
            *inspect(
                "library",
                {"search": search, "object_type": object_type, "after": after, "limit": limit},
            )
        )

    @routes.get("/{kind}/{identity}")
    def detail(
        kind: Kind,
        identity: str,
        operation: Literal["connections", "content"] = "connections",
        after: str | None = Query(default=None, max_length=240),
        relationship: str | None = Query(default=None, max_length=120),
        field: str = Query(default="summary", max_length=60),
        offset: int = Query(default=0, ge=0, le=100000000),
        characters: int = Query(default=4000, ge=1, le=16000),
    ):
        if not 1 <= len(identity) <= 200:
            raise HTTPException(422, "Invalid memory identity.")
        return _detail_view(
            *inspect(
                "explore",
                {
                    "object_type": kind,
                    "object_id": identity,
                    "operation": operation,
                    "after": after,
                    "relationship": relationship,
                    "field": field,
                    "offset": offset,
                    "characters": characters,
                },
            )
        )

    return routes
