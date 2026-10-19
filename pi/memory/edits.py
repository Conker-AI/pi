"""Owner text corrections coordinated with recall; MemoryGate is the only writer."""

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import Field, field_validator

from pi.core.errors import ServiceError
from pi.core.models import Strict
from pi.memory import store as memory_store
from pi.memory.corrections import CorrectionError

MEMORY_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}"
REQUEST_ID = r"[A-Za-z0-9_-]{16,128}"
UNRESOLVED = "('applying','unknown')"
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_edit_operations (
 request_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 destination TEXT NOT NULL, expected_revision INTEGER NOT NULL,
 payload_hash TEXT NOT NULL, remote_payload_hash TEXT NOT NULL, desired TEXT,
 state TEXT NOT NULL CHECK(state IN ('applying','unknown','applied','rejected')),
 error_code TEXT, receipt TEXT, cached_packages_cleared INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_edit_one_pending
ON memory_edit_operations((1)) WHERE state IN ('applying','unknown');
"""


class Apply(Strict):
    request_id: str = Field(pattern=rf"^{REQUEST_ID}$")
    memory_id: str = Field(pattern=rf"^{MEMORY_ID}$")
    expected_revision: int = Field(ge=1, le=2147483646)
    text: str = Field(min_length=1, max_length=16000)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Provide nonblank text without NUL.")
        value.encode("utf-8")
        return value


class Retry(Strict):
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditError(ServiceError):
    pass


def _error(code, message, status=409):
    raise EditError(code, message, status)


def _id(value, pattern):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        _error("invalid_request", "Invalid memory correction identity.", 422)
    return value


def _configured(client):
    if client is None:
        _error("not_configured", "Companion memory correction is not configured.", 503)


def _binding(row, client):
    _configured(client)
    if (row["agent_id"], row["destination"]) != (client.agent_id, client.destination):
        _error("request_conflict", "Use the original memory configuration for this correction.")


def _row(db, identity):
    row = db.execute(
        "SELECT * FROM memory_edit_operations WHERE request_id=?", (identity,)
    ).fetchone()
    if row is None:
        _error("not_found", "Memory correction not found.", 404)
    return row


def _view(row):
    receipt = json.loads(row["receipt"]) if row["receipt"] else None
    return {
        "schemaVersion": 1,
        "requestId": row["request_id"],
        "memoryId": row["memory_id"],
        "previousRevision": row["expected_revision"],
        "payloadHash": row["payload_hash"],
        "status": row["state"],
        "revision": receipt["revision"] if receipt else None,
        "indexing": receipt["indexing"] if receipt else None,
        "errorCode": row["error_code"],
        "cachedPackagesCleared": row["cached_packages_cleared"],
        "sourceConversationKept": True,
    }


def get(store, identity):
    with store._connect() as db:
        return _view(_row(db, _id(identity, REQUEST_ID)))


def pending(store):
    with store._connect() as db:
        row = db.execute(
            f"SELECT * FROM memory_edit_operations WHERE state IN {UNRESOLVED} LIMIT 1"
        ).fetchone()
        return {
            "schemaVersion": 1,
            "pending": {
                "requestId": row["request_id"],
                "memoryId": row["memory_id"],
                "expectedRevision": row["expected_revision"],
                "payloadHash": row["payload_hash"],
                "status": row["state"],
            }
            if row
            else None,
        }


def preview(client, identity):
    _id(identity, MEMORY_ID)
    _configured(client)
    try:
        value = client.memory(identity)
        if (
            value["revision"] > 2147483646
            or not isinstance(value["source_type"], str)
            or not isinstance(value["confidence"], str)
            or len(value["source_type"]) > 100
            or len(value["confidence"]) > 100
            or "\x00" in value["text"]
        ):
            raise ValueError()
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except CorrectionError as exc:
        if exc.status == 404:
            _error("not_found", "Editable Companion memory not found.", 404)
        _error("unavailable", "The authoritative memory cannot be loaded.", 503)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        _error("unavailable", "The authoritative memory cannot be loaded.", 503)
    return {
        "schemaVersion": 1,
        "memoryId": value["id"],
        "revision": value["revision"],
        "text": value["text"],
        "sourceType": value["source_type"],
        "confidence": value["confidence"],
    }


_active_lock = threading.Lock()
_active = set()


@contextmanager
def _exclusive(store):
    with _active_lock:
        if store.path in _active:
            _error("in_progress", "A memory correction is in progress. Check its status.")
        _active.add(store.path)
    try:
        yield
    finally:
        with _active_lock:
            _active.discard(store.path)


def _digest(body):
    return hashlib.sha256(
        json.dumps(
            body.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def remote_digest(agent_id, body):
    """MemoryGate's exact namespace-bound body fingerprint, excluding request ID."""
    return hashlib.sha256(
        json.dumps(
            {
                "agent_id": agent_id,
                "memory_id": body.memory_id,
                "expected_revision": body.expected_revision,
                "text": body.text,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _admit(store, client, body, digest):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if _source_deleted(db, client.agent_id, body.memory_id):
            _error("not_found", "The memory's source conversation was forgotten.", 404)
        if (
            db.execute("SELECT 1 FROM memory_forget_operations WHERE state='pending'").fetchone()
            or db.execute(
                f"SELECT 1 FROM memory_edit_operations WHERE state IN {UNRESOLVED}"
            ).fetchone()
        ):
            _error("change_pending", "Resolve the previous memory change first. Recall is paused.")
        if (
            db.execute("SELECT 1 FROM turns WHERE status='running' LIMIT 1").fetchone()
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE state='preparing' LIMIT 1"
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM background_thinking WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
        ):
            _error(
                "work_active", "Finish or stop current work before editing memory. Nothing changed."
            )
        db.execute(
            "INSERT INTO memory_edit_operations"
            "(request_id,memory_id,agent_id,destination,expected_revision,payload_hash,remote_payload_hash,desired,state)"
            " VALUES(?,?,?,?,?,?,?,?,'applying')",
            (
                body.request_id,
                body.memory_id,
                client.agent_id,
                client.destination,
                body.expected_revision,
                digest,
                remote_digest(client.agent_id, body),
                body.text,
            ),
        )
        cleared = memory_store.invalidate_recall(db)
        db.execute(
            "UPDATE memory_edit_operations SET cached_packages_cleared=? WHERE request_id=?",
            (cleared, body.request_id),
        )
        db.commit()


def _receipt(row, value):
    if (
        not isinstance(value, dict)
        or value.get("request_id") != row["request_id"]
        or value.get("memory_id") != row["memory_id"]
        or value.get("agent_id") != row["agent_id"]
        or value.get("payload_hash") != row["remote_payload_hash"]
        or type(value.get("previous_revision")) is not int
        or value["previous_revision"] != row["expected_revision"]
        or type(value.get("revision")) is not int
        or value["revision"] != row["expected_revision"] + 1
        or value.get("status") != "applied"
        or value.get("indexing") not in {"pending", "indexed", "degraded"}
    ):
        raise ValueError("Invalid correction receipt.")
    return {"revision": value["revision"], "indexing": value["indexing"]}


def _finish(store, identity, *, receipt=None, rejection=None):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        if row["state"] not in {"applying", "unknown"}:
            return _view(row)
        cleared = memory_store.invalidate_recall(db)
        db.execute(
            "UPDATE memory_edit_operations SET state=?,desired=NULL,receipt=?,error_code=?,"
            "cached_packages_cleared=cached_packages_cleared+? WHERE request_id=?",
            (
                "applied" if receipt else "rejected",
                json.dumps(receipt) if receipt else None,
                rejection,
                cleared,
                identity,
            ),
        )
        result = _view(_row(db, identity))
        db.commit()
        return result


def _unknown(store, identity):
    with store._connect() as db:
        db.execute(
            "UPDATE memory_edit_operations SET state='unknown' WHERE request_id=? AND state='applying'",
            (identity,),
        )
    return get(store, identity)


def _dispatch(store, client, identity, *, fresh):
    with store._connect() as db:
        row = _row(db, identity)
        if row["state"] not in {"applying", "unknown"} or row["desired"] is None:
            return _view(row)
    try:
        approved = Apply(
            request_id=identity,
            memory_id=row["memory_id"],
            expected_revision=row["expected_revision"],
            text=row["desired"],
        )
        if (
            _digest(approved) != row["payload_hash"]
            or remote_digest(row["agent_id"], approved) != row["remote_payload_hash"]
        ):
            raise ValueError()
    except ValueError:
        _error("request_conflict", "The saved approved correction could not be verified.")
    try:
        value = client.apply(identity, row["memory_id"], row["expected_revision"], row["desired"])
        receipt = _receipt(row, value)
    except CorrectionError as exc:
        # Pinned MemoryGate rechecks the permanent receipt under its target lock.
        # An exact retry409 therefore cannot hide an earlier matching commit: that
        # commit would return its receipt200, even after later edits or forgetting.
        if exc.status == 409 or (fresh and exc.status in {400, 401, 403, 404, 422}):
            code = {404: "not_found", 409: "revision_conflict"}.get(exc.status, "unavailable")
            return _finish(store, identity, rejection=code)
        return _unknown(store, identity)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return _unknown(store, identity)
    return _finish(store, identity, receipt=receipt)


def apply(store, client, body):
    body = Apply.model_validate(body.model_dump())
    _configured(client)
    digest = _digest(body)
    with _exclusive(store):
        with store._connect() as db:
            old = db.execute(
                "SELECT * FROM memory_edit_operations WHERE request_id=?", (body.request_id,)
            ).fetchone()
            if old:
                _binding(old, client)
                if old["payload_hash"] != digest:
                    _error(
                        "request_conflict", "Correction identity was already used for another edit."
                    )
                return _view(old)  # Duplicate submit never repeats a remote mutation.
        current = preview(client, body.memory_id)
        if current["revision"] != body.expected_revision:
            _error("revision_conflict", "Memory changed. Review the current version first.")
        _admit(store, client, body, digest)
        return _dispatch(store, client, body.request_id, fresh=True)


def status(store, client, identity):
    _id(identity, REQUEST_ID)
    with store._connect() as db:
        row = _row(db, identity)
    _binding(row, client)
    if row["state"] not in {"applying", "unknown"}:
        return _view(row)
    try:
        receipt = _receipt(row, client.receipt(identity))
    except (CorrectionError, httpx.HTTPError, ValueError, TypeError, KeyError):
        # In particular, receipt404 does not prove that no remote commit can still happen.
        return get(store, identity)
    return _finish(store, identity, receipt=receipt)


def retry(store, client, identity, body):
    _id(identity, REQUEST_ID)
    body = Retry.model_validate(body.model_dump())
    with _exclusive(store):
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = _row(db, identity)
            _binding(row, client)
            if row["payload_hash"] != body.expected_payload_hash:
                _error("request_conflict", "The approved correction identity changed.")
            if row["state"] not in {"applying", "unknown"}:
                return _view(row)
            if row["desired"] is None:
                _error("request_conflict", "The approved correction payload is unavailable.")
            db.execute(
                "UPDATE memory_edit_operations SET state='applying' WHERE request_id=?", (identity,)
            )
            db.commit()
        return _dispatch(store, client, identity, fresh=False)


def recover_interrupted(store):
    with store._connect() as db:
        db.execute("UPDATE memory_edit_operations SET state='unknown' WHERE state='applying'")


def redact_memory(db, agent_id, destination, memory_id):
    """A verified memory forget must not retain another pending copy of its text."""
    db.execute(
        "UPDATE memory_edit_operations SET desired=NULL,state='rejected',error_code='memory_forgotten' "
        f"WHERE agent_id=? AND destination=? AND memory_id=? AND state IN {UNRESOLVED}",
        (agent_id, destination, memory_id),
    )


def _source_function(db, agent_id):
    # Same immutable IDs as MemoryGate's conversation ingestion/receipt contract.
    # No message content or unbounded result collection is loaded into Python.
    def identity(message_id):
        receipt = str(uuid5(NAMESPACE_URL, f"pi:{agent_id}:{message_id}"))
        return str(uuid5(NAMESPACE_URL, receipt + ":memory"))

    db.create_function("conker_edit_source_id", 1, identity, deterministic=True)


def _source_deleted(db, agent_id, memory_id):
    _source_function(db, agent_id)
    return (
        db.execute(
            "SELECT 1 FROM memory_outbox o WHERE o.operation='delete' "
            "AND (o.destination_agent_id=? OR o.destination_agent_id IS NULL) "
            "AND conker_edit_source_id(o.message_id)=? LIMIT 1",
            (agent_id, memory_id),
        ).fetchone()
        is not None
    )


def redact_sources(db, session_ids):
    # One unresolved operation globally. Historical receipts may have been cleared;
    # deterministic conversation IDs still bind the memory to the forgotten source.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_edit_operations'").fetchone():
        return  # Offline forgetting must also work before this additive feature was installed.
    rows = db.execute(
        f"SELECT request_id,memory_id,agent_id FROM memory_edit_operations WHERE state IN {UNRESOLVED}"
    ).fetchall()
    for row in rows:
        _source_function(db, row["agent_id"])
        for sid in session_ids:
            match = db.execute(
                "SELECT 1 FROM memory_outbox o JOIN messages m ON m.id=o.message_id "
                "WHERE m.session_id=? AND (o.destination_agent_id=? OR o.destination_agent_id IS NULL) "
                "AND conker_edit_source_id(o.message_id)=? LIMIT 1",
                (sid, row["agent_id"], row["memory_id"]),
            ).fetchone()
            if match:
                db.execute(
                    "UPDATE memory_edit_operations SET desired=NULL,state='rejected',"
                    "error_code='memory_forgotten' WHERE request_id=?",
                    (row["request_id"],),
                )
                break
