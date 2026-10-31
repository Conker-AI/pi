"""Owner-reviewed forgetting of one memory, and of Pi's cached copies of it.

MemoryGate removes the memory, its history and its audit text (see its
`memory_forgetting`). A durable recall hold covers uncertain remote outcomes.
Historical briefing copies lack IDs, so all derived recall caches are invalidated.
The source conversation is not changed: forget the chat separately.
"""

import json
import threading

import httpx
from pydantic import Field

from pi.core.errors import ServiceError
from pi.core.models import Strict
from pi.memory import store as memory_store
from pi.memory.corrections import CorrectionError


class Forget(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    memory_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
    expected_revision: int = Field(ge=1, le=2147483646)


class ForgetError(ServiceError):
    pass


_active_lock = threading.Lock()
_active = set()


def _begin(store, client, body):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM memory_forget_operations WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if old and (
            old["memory_id"],
            old["agent_id"],
            old["destination"],
            old["expected_revision"],
        ) != (
            body.memory_id,
            client.agent_id,
            client.destination,
            body.expected_revision,
        ):
            raise ForgetError("request_conflict", "Forget request identity was already used.", 409)
        if old and old["state"] == "forgotten":
            return json.loads(old["receipt"]), False
        if db.execute(
            "SELECT 1 FROM memory_edit_operations WHERE state IN ('applying','unknown')"
        ).fetchone():
            raise ForgetError(
                "forget_pending", "Resolve the pending memory correction before forgetting.", 409
            )
        if db.execute(
            "SELECT 1 FROM memory_forget_operations WHERE state='pending' AND request_id!=?",
            (body.request_id,),
        ).fetchone():
            raise ForgetError(
                "forget_pending", "Resolve the earlier forget request first. Recall is paused.", 409
            )
        fresh = old is None or old["state"] != "pending"
        if fresh and (
            db.execute("SELECT 1 FROM turns WHERE status='running' LIMIT 1").fetchone()
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE state='preparing' LIMIT 1"
            ).fetchone()
        ):
            raise ForgetError(
                "work_active",
                "Finish or stop current work, then retry forgetting. Nothing was changed.",
                409,
            )
        if old is None:
            db.execute(
                "INSERT INTO memory_forget_operations"
                "(request_id,memory_id,agent_id,destination,expected_revision,state) VALUES(?,?,?,?,?,'pending')",
                (
                    body.request_id,
                    body.memory_id,
                    client.agent_id,
                    client.destination,
                    body.expected_revision,
                ),
            )
        else:
            db.execute(
                "UPDATE memory_forget_operations SET state='pending' WHERE request_id=?",
                (body.request_id,),
            )
        cleared = memory_store.invalidate_recall(db)
        db.execute(
            "UPDATE memory_forget_operations SET cached_packages_cleared="
            "cached_packages_cleared+? WHERE request_id=?",
            (cleared, body.request_id),
        )
        db.commit()
    return None, fresh


def _refused(store, body, fresh):
    # A refusal of a recovery attempt cannot resolve a previous uncertain outcome.
    if fresh:
        with store._connect() as db:
            db.execute(
                "UPDATE memory_forget_operations SET state='rejected' "
                "WHERE request_id=? AND state='pending'",
                (body.request_id,),
            )


def _unknown():
    return ForgetError(
        "outcome_unknown",
        "The forget outcome is unknown. Recall is paused; ordinary conversation may continue. "
        "Repeat this exact request to check; it cannot apply twice.",
        503,
    )


def pending(store):
    """Content-free recovery identity, available even after the remote record is gone."""
    with store._connect() as db:
        row = db.execute(
            "SELECT request_id,memory_id,expected_revision FROM memory_forget_operations "
            "WHERE state='pending' ORDER BY request_id LIMIT 1"
        ).fetchone()
    return {
        "schemaVersion": 1,
        "pending": {
            "requestId": row["request_id"],
            "memoryId": row["memory_id"],
            "revision": row["expected_revision"],
        }
        if row
        else None,
    }


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
    body = Forget.model_validate(body.model_dump())
    if client is None:
        raise ForgetError(
            "not_configured",
            "Forgetting needs the memory correction capability (PI_MEMORY_CORRECTION_*).",
            503,
        )
    with _active_lock:
        if store.path in _active:
            raise ForgetError(
                "forget_in_progress",
                "A forget request is in progress; retry after it finishes.",
                409,
            )
        _active.add(store.path)
    try:
        prior, fresh = _begin(store, client, body)
        if prior is not None:
            return prior
        try:
            receipt = client.forget(body.request_id, body.memory_id, body.expected_revision)
        except CorrectionError as exc:
            if exc.status not in {400, 401, 403, 404, 409, 422} or not fresh:
                raise _unknown() from None
            _refused(store, body, fresh)
            if exc.status == 404:
                raise ForgetError("not_found", "Memory not found.", 404) from None
            if exc.status == 409:
                raise ForgetError(
                    "revision_conflict",
                    "The memory changed; review it again before forgetting.",
                    409,
                ) from None
            raise ForgetError("unavailable", "Memory service refused the request.", 503) from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise _unknown() from None
        if (
            not isinstance(receipt, dict)
            or receipt.get("request_id") != body.request_id
            or receipt.get("agent_id") != client.agent_id
            or receipt.get("memory_id") != body.memory_id
            or type(receipt.get("revision")) is not int
            or receipt["revision"] != body.expected_revision
            or receipt.get("status") != "forgotten"
            or receipt.get("index_removal") not in {"removed", "degraded", "pending"}
        ):
            raise ForgetError(
                "invalid_receipt",
                "Forget receipt could not be verified. Recall remains paused.",
                502,
            )
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from pi.memory import edits as memory_edits
            from pi.memory import proposals as memory_proposals

            memory_edits.redact_memory(db, client.agent_id, client.destination, body.memory_id)
            memory_proposals.redact_memory(db, client.agent_id, body.memory_id)
            cleared = memory_store.invalidate_recall(db)
            old = db.execute(
                "SELECT cached_packages_cleared FROM memory_forget_operations WHERE request_id=?",
                (body.request_id,),
            ).fetchone()
            result = {
                "requestId": body.request_id,
                "memoryId": body.memory_id,
                "status": "forgotten",
                "indexRemoval": receipt["index_removal"],
                "cachedPackagesCleared": old[0] + cleared,
                "sourceConversationKept": True,
            }
            db.execute(
                "UPDATE memory_forget_operations SET state='forgotten',cached_packages_cleared=?,"
                "receipt=? WHERE request_id=?",
                (result["cachedPackagesCleared"], json.dumps(result), body.request_id),
            )
            db.commit()
        return result
    finally:
        with _active_lock:
            _active.discard(store.path)
