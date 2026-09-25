"""Owner-reviewed forgetting of one memory, and of Pi's cached copies of it.

MemoryGate removes the memory, its history and its audit text (see its
`memory_forgetting`). Pi then clears every cached per-turn memory package that
quoted it. The source conversation is not changed: forget the chat separately.
"""

import json

import httpx
from pydantic import Field

from . import agents
from .memory_corrections import CorrectionError


class Forget(agents.StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    memory_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
    expected_revision: int = Field(ge=1)


class ForgetError(Exception):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _redact_cached(store, memory_id: str) -> int:
    redacted = 0
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT turn_id, package FROM memory_contexts WHERE package LIKE ?",
            ("%" + memory_id + "%",),
        ).fetchall()
        for row in rows:
            package = json.loads(row["package"])
            if any(item.get("id") == memory_id for item in package.get("memories", [])):
                db.execute(
                    "UPDATE memory_contexts SET package=NULL, status='redacted' WHERE turn_id=?",
                    (row["turn_id"],),
                )
                redacted += 1
        db.commit()
    return redacted


def preview(client, memory_id: str) -> dict:
    """The exact current text and revision the owner is about to forget."""
    if client is None:
        raise ForgetError(
            "not_configured",
            "Forgetting needs the memory correction capability (PI_MEMORY_CORRECTION_*).",
            503,
        )
    try:
        current = client.memory(memory_id)
    except CorrectionError as exc:
        if exc.status == 404:
            raise ForgetError("not_found", "Memory not found.", 404) from None
        raise ForgetError("unavailable", "Memory service is unavailable.", 503) from None
    except (httpx.HTTPError, ValueError):
        raise ForgetError("unavailable", "Memory service is unavailable.", 503) from None
    return {"memoryId": current["id"], "revision": current["revision"], "text": current["text"]}


def forget(store, client, body: Forget) -> dict:
    if client is None:
        raise ForgetError(
            "not_configured",
            "Forgetting needs the memory correction capability (PI_MEMORY_CORRECTION_*).",
            503,
        )
    try:
        receipt = client.forget(body.request_id, body.memory_id, body.expected_revision)
    except CorrectionError as exc:
        if exc.status == 404:
            raise ForgetError("not_found", "Memory not found.", 404) from None
        if exc.status == 409:
            raise ForgetError(
                "revision_conflict", "The memory changed; review it again before forgetting.", 409
            ) from None
        raise ForgetError(
            "unavailable", "Memory service refused or is unavailable. Nothing was confirmed.", 503
        ) from None
    except (httpx.HTTPError, ValueError):
        # A lost reply is not proof either way; the same request id is safe to repeat.
        raise ForgetError(
            "outcome_unknown",
            "The outcome is unknown. Repeat the same request to check; it cannot apply twice.",
            503,
        ) from None
    if receipt.get("memory_id") != body.memory_id or receipt.get("status") != "forgotten":
        raise ForgetError("invalid_receipt", "Memory service returned an unexpected receipt.", 502)
    return {
        "requestId": body.request_id,
        "memoryId": body.memory_id,
        "status": "forgotten",
        "indexRemoval": receipt.get("index_removal"),
        "cachedPackagesCleared": _redact_cached(store, body.memory_id),
        "sourceConversationKept": True,
    }
