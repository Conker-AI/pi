"""What Conker believes about the owner (R-M5): one page of MemoryGate's memory-beliefs list.

MemoryGate's `GET /runtime/memory-beliefs` reads one agent's current memories with their origin,
confidence, evidence counts, lineage and open conflicts, and writes nothing. Pi asks it with the
companion's read key (the pair recall uses), passes the filters and the cursor on unchanged and
answers in camelCase. Two kinds of memory are left out before the owner sees the page:

- a memory Pi has a forget pending for, or has already forgotten (`memory_forget_operations`), so
  a forget in flight never shows as still believed;
- a memory made from a hidden chat (forgotten or in the Trash), by the same set recall and the
  memory library use (`pi.memory.hidden.hidden_objects`).

`total` and `counts` are MemoryGate's numbers for every page under the same filters, lowered only
by the rows left out of this page: rows on other pages that would be left out are not known here.

Fix, Forget and Remember need no route of their own: `POST /memory/corrections` with the item's
`revision`, `POST /memory/forget` (or `/memory/forget-many`) and `POST /memory/remember`.
Design: context/design/memory-beliefs.md in the Conker workspace.
"""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import ValidationError

from pi.core.errors import ServiceError
from pi.core.models import Strict
from pi.memory.hidden import hidden_objects

MAX_REPLY_BYTES = 1024 * 1024
UNAVAILABLE = "Memory didn't answer, so Conker can't show what it believes. Try again."

Origin = Literal["stated", "concluded"]
State = Literal["current", "needs_review"]


class BeliefEvidence(Strict):
    supporting: int
    withdrawn: int


class BeliefLineage(Strict):
    objectType: str
    objectId: str
    withdrawn: bool


class BeliefConflict(Strict):
    conflictId: str
    memoryId: str


class Belief(Strict):
    id: str
    revision: int
    text: str
    memoryType: str
    origin: Origin
    sourceType: str | None
    # The stored string (high, medium or low), passed on as it is.
    confidence: str | None
    state: State
    validFrom: str | None
    validUntil: str | None
    reviewBy: str | None
    createdAt: str | None
    updatedAt: str | None
    lastRecalledAt: str | None
    evidence: BeliefEvidence
    lineage: list[BeliefLineage]
    conflicts: list[BeliefConflict]


class BeliefCounts(Strict):
    stated: int
    concluded: int
    needsReview: int


class BeliefPage(Strict):
    agentId: str
    total: int
    counts: BeliefCounts
    items: list[Belief]
    nextAfter: str | None


class BeliefsError(ServiceError):
    pass


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _camel_keys(value):
    if isinstance(value, dict):
        return {_camel(key): _camel_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_camel_keys(item) for item in value]
    return value


def _fetch(client, params: dict) -> dict:
    """MemoryGate's page for the client's own agent; a 422 stays a 422, anything else is 503."""
    query = {key: value for key, value in params.items() if value is not None}
    try:
        with client.http.stream(
            "GET", "/runtime/memory-beliefs", headers=client.read_headers, params=query
        ) as response:
            if response.status_code == 422:
                raise BeliefsError(
                    "invalid_request", "Memory refused these filters or this page cursor.", 422
                )
            if response.status_code != 200:
                raise BeliefsError("memory_unavailable", UNAVAILABLE, 503)
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAX_REPLY_BYTES:
                    raise BeliefsError("memory_unavailable", UNAVAILABLE, 503)
                raw.extend(chunk)
        result = json.loads(raw)
    except BeliefsError:
        raise
    except (httpx.HTTPError, ValueError, RecursionError):
        raise BeliefsError("memory_unavailable", UNAVAILABLE, 503) from None
    if not isinstance(result, dict) or result.get("agent_id") != client.agent_id:
        raise BeliefsError("memory_unavailable", UNAVAILABLE, 503)
    return result


def _left_out(store, agent_id: str) -> set[str]:
    """Memory ids the owner must not see as believed: forgets pending or done, hidden chats."""
    if store is None:
        return set()
    with store._connect() as db:
        forgetting = {
            row[0]
            for row in db.execute(
                "SELECT memory_id FROM memory_forget_operations WHERE state IN "
                "('pending','forgotten')"
            )
        }
        hidden = hidden_objects(db, (agent_id,)).get("memory", set())
    return forgetting | hidden


def _without(page: BeliefPage, gone: set[str]) -> BeliefPage:
    dropped = [item for item in page.items if item.id in gone]
    if not dropped:
        return page
    stated = sum(item.origin == "stated" for item in dropped)
    review = sum(item.state == "needs_review" for item in dropped)
    counts = page.counts
    return page.model_copy(
        update={
            "items": [item for item in page.items if item.id not in gone],
            "total": max(0, page.total - len(dropped)),
            "counts": BeliefCounts(
                stated=max(0, counts.stated - stated),
                concluded=max(0, counts.concluded - (len(dropped) - stated)),
                needsReview=max(0, counts.needsReview - review),
            ),
        }
    )


def beliefs(service, params: dict) -> dict:
    """One page of beliefs; MemoryGate missing or not answering is 503, never an empty list."""
    client = getattr(service, "client", None)
    if client is None:
        raise BeliefsError(
            "memory_unavailable", "Memory isn't connected, so there is nothing to show.", 503
        )
    reply = _fetch(client, params)
    try:
        page = BeliefPage.model_validate(_camel_keys(reply))
    except ValidationError:
        raise BeliefsError("memory_unavailable", UNAVAILABLE, 503) from None
    page = _without(page, _left_out(getattr(service, "store", None), client.agent_id))
    return page.model_dump()
