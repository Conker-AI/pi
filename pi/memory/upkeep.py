"""Memory upkeep candidates (R-M4): what could be merged, resolved, retired or checked.

MemoryGate's `GET /runtime/memory-upkeep` lists, for one agent, exact and near duplicates, open
contradictions, expired memories and ones not recalled for a while, each with its ids and a
reason, plus every listed memory's words and revision. It writes nothing. Pi asks it with the
companion's read key (the pair recall uses) and answers in camelCase.

A candidate that names a memory the owner must not see is left out whole: a forget pending or done
(`memory_forget_operations`), or a memory made from a hidden chat (forgotten or in the Trash, the
set recall and the memory library use). Half a duplicate pair says nothing useful and still points
at the hidden one. Each kind's `total` drops only by the items left out of this reply.

Acting on a candidate needs no route of its own: Fix is `POST /memory/corrections` with the
memory's `revision`, Forget is `POST /memory/forget` (or `/memory/forget-many`).
Brief: context/briefs/memory-upkeep-pi-side.md in the Conker workspace.
"""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import Field, ValidationError

from pi.core.errors import ServiceError
from pi.core.models import Strict
from pi.memory.beliefs import MAX_REPLY_BYTES, _camel, _camel_keys, _left_out

UNAVAILABLE = "Memory didn't answer, so Conker can't check its memories. Try again."
KINDS = ("exactDuplicates", "nearDuplicates", "contradictions", "expired", "notRecalled")


class UpkeepItem(Strict):
    ids: list[str]
    reason: str
    # Only contradictions name the open conflict behind them.
    conflictId: str | None = None


class UpkeepKind(Strict):
    items: list[UpkeepItem]
    total: int


class UpkeepMemory(Strict):
    text: str
    revision: int
    memoryType: str | None
    sourceType: str | None
    state: Literal["current", "needs_review"]


class UpkeepPage(Strict):
    agentId: str
    checkedAt: str
    memoriesChecked: int
    truncated: bool
    exactDuplicates: UpkeepKind
    nearDuplicates: UpkeepKind
    contradictions: UpkeepKind
    expired: UpkeepKind
    notRecalled: UpkeepKind
    # A MemoryGate from before the words were added sends none; the ids still stand.
    memories: dict[str, UpkeepMemory] = Field(default_factory=dict)


class UpkeepError(ServiceError):
    pass


def _fetch(client, params: dict) -> dict:
    """MemoryGate's candidates for the client's own agent; a 422 stays a 422, anything else 503."""
    try:
        with client.http.stream(
            "GET", "/runtime/memory-upkeep", headers=client.read_headers, params=params
        ) as response:
            if response.status_code == 422:
                raise UpkeepError("invalid_request", "Memory refused these settings.", 422)
            if response.status_code != 200:
                raise UpkeepError("memory_unavailable", UNAVAILABLE, 503)
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAX_REPLY_BYTES:
                    raise UpkeepError("memory_unavailable", UNAVAILABLE, 503)
                raw.extend(chunk)
        result = json.loads(raw)
    except UpkeepError:
        raise
    except (httpx.HTTPError, ValueError, RecursionError):
        raise UpkeepError("memory_unavailable", UNAVAILABLE, 503) from None
    if not isinstance(result, dict) or result.get("agent_id") != client.agent_id:
        raise UpkeepError("memory_unavailable", UNAVAILABLE, 503)
    return result


def _without(page: UpkeepPage, gone: set[str]) -> UpkeepPage:
    update: dict = {}
    for kind in KINDS:
        group: UpkeepKind = getattr(page, kind)
        kept = [item for item in group.items if not gone.intersection(item.ids)]
        if len(kept) != len(group.items):
            dropped = len(group.items) - len(kept)
            update[kind] = UpkeepKind(items=kept, total=max(0, group.total - dropped))
    named = {
        memory_id
        for kind in KINDS
        for item in (update.get(kind) or getattr(page, kind)).items
        for memory_id in item.ids
    }
    memories = {key: value for key, value in page.memories.items() if key in named}
    if len(memories) != len(page.memories):
        update["memories"] = memories
    return page.model_copy(update=update) if update else page


def upkeep(service, params: dict) -> dict:
    """Upkeep candidates; MemoryGate missing or not answering is 503, never an empty list."""
    client = getattr(service, "client", None)
    if client is None:
        raise UpkeepError(
            "memory_unavailable", "Memory isn't connected, so there is nothing to check.", 503
        )
    reply = _fetch(client, params)
    # The `memories` map is keyed by memory id: rename the fields inside, never the ids.
    named = reply.pop("memories", None)
    body = {_camel(key): _camel_keys(value) for key, value in reply.items()}
    if isinstance(named, dict):
        body["memories"] = {key: _camel_keys(value) for key, value in named.items()}
    elif named is not None:
        body["memories"] = named
    try:
        page = UpkeepPage.model_validate(body)
    except ValidationError:
        raise UpkeepError("memory_unavailable", UNAVAILABLE, 503) from None
    page = _without(page, _left_out(getattr(service, "store", None), client.agent_id))
    return page.model_dump()
