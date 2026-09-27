"""Evidence-backed first-run rehearsal using real, harmless owner workflows."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import setup_choices, setup_receipts
from .toolgate import ApprovalRequired, ToolPending, ToolRefused, ToolResult

REQUEST_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
TOOL_ID = "approval.test-echo"
TOOL_ARGUMENTS = {"value": "Conker first-run approval rehearsal"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_rehearsal_memory_reviews (
    revision          INTEGER PRIMARY KEY CHECK(revision > 0),
    request_id        TEXT NOT NULL UNIQUE,
    choice_revision   INTEGER NOT NULL CHECK(choice_revision > 0),
    choice            TEXT NOT NULL CHECK(choice IN ('include','skip')),
    service_state     TEXT NOT NULL CHECK(service_state IN ('available','off')),
    evidence_digest   TEXT NOT NULL UNIQUE,
    reviewed_at       REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS setup_rehearsal_memory_reviews_no_update
BEFORE UPDATE ON setup_rehearsal_memory_reviews
BEGIN SELECT RAISE(ABORT, 'setup rehearsal memory reviews are append-only'); END;
CREATE TRIGGER IF NOT EXISTS setup_rehearsal_memory_reviews_no_delete
BEFORE DELETE ON setup_rehearsal_memory_reviews
BEGIN SELECT RAISE(ABORT, 'setup rehearsal memory reviews are append-only'); END;

CREATE TABLE IF NOT EXISTS setup_rehearsal_approvals (
    request_id          TEXT PRIMARY KEY,
    action_id           TEXT NOT NULL UNIQUE,
    approval_request_id TEXT UNIQUE,
    state               TEXT NOT NULL CHECK(state IN (
                            'dispatching','awaiting_owner','resuming','complete',
                            'refused','outcome_unknown','invalid_policy')),
    expires_at          TEXT,
    evidence_digest     TEXT UNIQUE,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS setup_rehearsal_approvals_latest
    ON setup_rehearsal_approvals(updated_at DESC);
"""


class RehearsalError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryReviewInput(StrictModel):
    requestId: str = Field(min_length=1, max_length=128, pattern=REQUEST_ID.pattern)
    expectedChoiceRevision: int = Field(gt=0)


class ApprovalInput(StrictModel):
    requestId: str = Field(min_length=1, max_length=128, pattern=REQUEST_ID.pattern)


class Phase(StrictModel):
    state: Literal[
        "missing",
        "complete",
        "awaiting_owner",
        "outcome_unknown",
        "refused",
        "invalid_policy",
    ]
    detail: str


class Status(StrictModel):
    schemaVersion: Literal[1] = 1
    state: Literal["in_progress", "ready", "complete"]
    conversation: Phase
    memoryReview: Phase
    approval: Phase
    approvalRequestId: str | None
    canFinalize: bool


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _conversation(db: sqlite3.Connection):
    return db.execute(
        "SELECT t.id,t.ended_at FROM turns t "
        "LEFT JOIN forgotten_sessions f ON f.session_id=t.session_id "
        "WHERE t.status='complete' AND t.ended_at IS NOT NULL AND f.session_id IS NULL "
        "ORDER BY t.ended_at DESC,t.id DESC LIMIT 1"
    ).fetchone()


def _memory_review(db: sqlite3.Connection, choice_revision: int):
    return db.execute(
        "SELECT * FROM setup_rehearsal_memory_reviews WHERE choice_revision=? "
        "ORDER BY revision DESC LIMIT 1",
        (choice_revision,),
    ).fetchone()


def _approval(db: sqlite3.Connection):
    return db.execute(
        "SELECT * FROM setup_rehearsal_approvals ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()


def review_memory(store, body: MemoryReviewInput, memory, *, now: float | None = None) -> Status:
    choice = setup_choices.current(store, "memory")
    if choice.choice not in {"include", "skip"}:
        raise RehearsalError(
            "memory_choice_required",
            "Choose whether new conversations may use long-term memory before reviewing it.",
            409,
        )
    if choice.revision != body.expectedChoiceRevision:
        raise RehearsalError("revision_conflict", f"Expected memory choice revision {choice.revision}.", 409)
    service_state = "off"
    if choice.choice == "include":
        try:
            healthy = memory.client is not None and memory.health().get("status") == "ok"
        except Exception:
            healthy = False
        if not healthy:
            raise RehearsalError(
                "memory_unavailable",
                "Long-term memory is selected but is not currently available for review.",
                503,
            )
        service_state = "available"
    reviewed = now if now is not None else time.time()
    evidence = {
        "kind": "memory-review",
        "choice": choice.choice,
        "choiceRevision": choice.revision,
        "serviceState": service_state,
    }
    digest = _digest(evidence)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        replay = db.execute(
            "SELECT * FROM setup_rehearsal_memory_reviews WHERE request_id=?",
            (body.requestId,),
        ).fetchone()
        if replay:
            if replay["evidence_digest"] != digest:
                db.execute("ROLLBACK")
                raise RehearsalError(
                    "replay_conflict", "requestId was already used for another memory review.", 409
                )
            db.execute("COMMIT")
            return status(store)
        revision = db.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM setup_rehearsal_memory_reviews"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO setup_rehearsal_memory_reviews VALUES (?,?,?,?,?,?,?)",
            (
                revision,
                body.requestId,
                choice.revision,
                choice.choice,
                service_state,
                digest,
                reviewed,
            ),
        )
        db.execute("COMMIT")
    return status(store)


def start_approval(store, body: ApprovalInput, toolgate, *, now: float | None = None) -> Status:
    if toolgate is None:
        raise RehearsalError("toolgate_unavailable", "ToolGate is not configured.", 503)
    observed = now if now is not None else time.time()
    action_id = "pi_setup_" + hashlib.sha256(body.requestId.encode()).hexdigest()[:32]
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        replay = db.execute(
            "SELECT action_id FROM setup_rehearsal_approvals WHERE request_id=?",
            (body.requestId,),
        ).fetchone()
        if replay:
            db.execute("COMMIT")
            if replay["action_id"] != action_id:
                raise RehearsalError("replay_conflict", "requestId was already used.", 409)
            return status(store)
        db.execute(
            "INSERT INTO setup_rehearsal_approvals "
            "(request_id,action_id,state,created_at,updated_at) VALUES (?,?,'dispatching',?,?)",
            (body.requestId, action_id, observed, observed),
        )
        db.execute("COMMIT")
    try:
        outcome = toolgate.invoke(TOOL_ID, TOOL_ARGUMENTS, action_id=action_id)
    except ToolRefused as exc:
        _set_approval(store, body.requestId, "refused", observed)
        raise RehearsalError("tool_refused", "ToolGate refused the rehearsal approval request.", 409) from exc
    if isinstance(outcome, ApprovalRequired) and outcome.tool_id == TOOL_ID and outcome.args == TOOL_ARGUMENTS:
        _set_approval(
            store,
            body.requestId,
            "awaiting_owner",
            observed,
            approval_request_id=outcome.request_id,
            expires_at=outcome.expires_at,
        )
    elif isinstance(outcome, ToolPending):
        _set_approval(store, body.requestId, "outcome_unknown", observed)
    else:
        _set_approval(store, body.requestId, "invalid_policy", observed)
    return status(store)


def resume_approval(store, body: ApprovalInput, toolgate, *, now: float | None = None) -> Status:
    if toolgate is None:
        raise RehearsalError("toolgate_unavailable", "ToolGate is not configured.", 503)
    observed = now if now is not None else time.time()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM setup_rehearsal_approvals WHERE request_id=?", (body.requestId,)
        ).fetchone()
        if row is None:
            db.execute("ROLLBACK")
            raise RehearsalError("approval_missing", "Start the rehearsal approval first.", 404)
        if row["state"] == "complete":
            db.execute("COMMIT")
            return status(store)
        if row["state"] != "awaiting_owner":
            db.execute("ROLLBACK")
            raise RehearsalError(
                "approval_not_resumable",
                "The rehearsal approval is not waiting for an owner decision.",
                409,
            )
        db.execute(
            "UPDATE setup_rehearsal_approvals SET state='resuming',updated_at=? WHERE request_id=?",
            (observed, body.requestId),
        )
        db.execute("COMMIT")
    try:
        outcome = toolgate.invoke(
            TOOL_ID,
            TOOL_ARGUMENTS,
            row["approval_request_id"],
            action_id=row["action_id"],
        )
    except ToolRefused as exc:
        _set_approval(store, body.requestId, "refused", observed)
        raise RehearsalError(
            "approval_refused", "The approval was declined, expired, or no longer matches.", 409
        ) from exc
    if isinstance(outcome, ToolResult) and outcome.ok is True and outcome.tool_id == TOOL_ID:
        evidence = {
            "kind": "harmless-approval",
            "toolId": TOOL_ID,
            "actionId": row["action_id"],
            "approvalRequestId": row["approval_request_id"],
        }
        _set_approval(
            store,
            body.requestId,
            "complete",
            observed,
            evidence_digest=_digest(evidence),
        )
    elif isinstance(outcome, ToolPending):
        _set_approval(store, body.requestId, "outcome_unknown", observed)
    else:
        _set_approval(store, body.requestId, "invalid_policy", observed)
    return status(store)


def _set_approval(
    store,
    request_id: str,
    state: str,
    observed: float,
    *,
    approval_request_id: str | None = None,
    expires_at: str | None = None,
    evidence_digest: str | None = None,
) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE setup_rehearsal_approvals SET state=?,"
            "approval_request_id=COALESCE(?,approval_request_id),"
            "expires_at=COALESCE(?,expires_at),"
            "evidence_digest=COALESCE(?,evidence_digest),updated_at=? WHERE request_id=?",
            (
                state,
                approval_request_id,
                expires_at,
                evidence_digest,
                observed,
                request_id,
            ),
        )


def status(store, *, now: datetime | None = None) -> Status:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    memory_choice = setup_choices.current(store, "memory")
    with store._connect() as db:
        conversation = _conversation(db)
        memory = _memory_review(db, memory_choice.revision)
        approval = _approval(db)
    receipt = setup_receipts.current(store, "rehearsal", now=observed)
    conversation_phase = Phase(
        state="complete" if conversation else "missing",
        detail=(
            "A completed companion conversation is available."
            if conversation
            else "Complete one conversation with your companion."
        ),
    )
    memory_phase = Phase(
        state="complete" if memory else "missing",
        detail=(
            "The current memory choice was reviewed."
            if memory
            else "Long-term memory is on for new conversations. Review this choice before continuing."
            if memory_choice.choice == "include"
            else "Long-term memory is off for new conversations. Review this choice before continuing."
            if memory_choice.choice == "skip"
            else "Choose whether new conversations may use long-term memory before reviewing it."
        ),
    )
    approval_state = approval["state"] if approval else "missing"
    projected = {
        "dispatching": "outcome_unknown",
        "resuming": "outcome_unknown",
        "awaiting_owner": "awaiting_owner",
        "complete": "complete",
        "refused": "refused",
        "outcome_unknown": "outcome_unknown",
        "invalid_policy": "invalid_policy",
        "missing": "missing",
    }[approval_state]
    details = {
        "missing": "Start the harmless local approval check.",
        "awaiting_owner": "Review the harmless local echo in Inbox, then finish the check here.",
        "complete": "The owner-approved local echo completed once.",
        "refused": "The approval was declined, expired, or no longer matches.",
        "outcome_unknown": "The approval outcome is uncertain and will not be repeated.",
        "invalid_policy": "The test tool did not require owner approval as expected.",
    }
    approval_phase = Phase(state=projected, detail=details[projected])
    ready = all(
        phase.state == "complete"
        for phase in (conversation_phase, memory_phase, approval_phase)
    )
    current_digest = _rehearsal_digest(conversation, memory, approval)
    complete = (
        receipt is not None
        and receipt.state == "valid"
        and receipt.evidenceDigest == current_digest
    )
    return Status(
        state="complete" if complete else "ready" if ready else "in_progress",
        conversation=conversation_phase,
        memoryReview=memory_phase,
        approval=approval_phase,
        approvalRequestId=approval["request_id"] if approval else None,
        canFinalize=ready and not complete,
    )


def finalize(store, *, now: datetime | None = None) -> setup_receipts.Receipt:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    memory_choice = setup_choices.current(store, "memory")
    with store._connect() as db:
        conversation = _conversation(db)
        memory = _memory_review(db, memory_choice.revision)
        approval = _approval(db)
    if conversation is None or memory is None or approval is None or approval["state"] != "complete":
        raise RehearsalError(
            "rehearsal_incomplete",
            "Complete one conversation, review memory, and finish the harmless approval flow.",
            409,
        )
    digest = _rehearsal_digest(conversation, memory, approval)
    current = setup_receipts.current(store, "rehearsal", now=observed)
    if current is not None and current.evidenceDigest == digest:
        return current
    completed = datetime.fromtimestamp(
        max(conversation["ended_at"], memory["reviewed_at"], approval["updated_at"]), UTC
    )
    value = setup_receipts.ReceiptInput(
        receiptId=f"rehearsal-{digest}",
        source="conker.first-run-rehearsal",
        subject="owner.daily-workflow",
        evidenceDigest=digest,
        completedAt=completed,
        expiresAt=completed + timedelta(days=30),
        expectedRevision=current.revision if current else 0,
    )
    try:
        return setup_receipts.record(store, "rehearsal", value, now=observed)
    except setup_receipts.ReceiptError as exc:
        raise RehearsalError(exc.code, exc.detail, exc.status) from exc


def _rehearsal_digest(conversation, memory, approval) -> str | None:
    if conversation is None or memory is None or approval is None or approval["state"] != "complete":
        return None
    return _digest(
        {
            "schema": "conker-first-run-rehearsal-1",
            "conversationTurnId": conversation["id"],
            "memoryReviewDigest": memory["evidence_digest"],
            "approvalDigest": approval["evidence_digest"],
        }
    )


def receipt_matches_current(store, receipt: setup_receipts.Receipt) -> bool:
    memory_choice = setup_choices.current(store, "memory")
    with store._connect() as db:
        digest = _rehearsal_digest(
            _conversation(db),
            _memory_review(db, memory_choice.revision),
            _approval(db),
        )
    return digest is not None and receipt.evidenceDigest == digest
