"""Durable owner attestations for setup work performed outside Pi."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReceiptStep = Literal["boundaries", "protection", "rehearsal"]
ReceiptState = Literal["valid", "stale"]

STEPS = ("boundaries", "protection", "rehearsal")
MAX_VALIDITY = {
    "boundaries": timedelta(days=30),
    "protection": timedelta(days=90),
    "rehearsal": timedelta(days=30),
}
IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,127}")
DIGEST = re.compile(r"[0-9a-f]{64}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_evidence_receipts (
    step             TEXT NOT NULL CHECK(step IN ('boundaries','protection','rehearsal')),
    revision         INTEGER NOT NULL CHECK(revision > 0),
    receipt_id       TEXT NOT NULL UNIQUE,
    source           TEXT NOT NULL,
    subject          TEXT NOT NULL,
    evidence_digest  TEXT NOT NULL UNIQUE,
    completed_at     REAL NOT NULL,
    expires_at       REAL,
    recorded_at      REAL NOT NULL,
    PRIMARY KEY (step, revision)
);
CREATE INDEX IF NOT EXISTS setup_evidence_receipts_latest
    ON setup_evidence_receipts(step, revision DESC);
CREATE TRIGGER IF NOT EXISTS setup_evidence_receipts_revision_sequence
BEFORE INSERT ON setup_evidence_receipts
WHEN NEW.revision != COALESCE(
    (SELECT MAX(revision) + 1 FROM setup_evidence_receipts WHERE step=NEW.step), 1
)
BEGIN
    SELECT RAISE(ABORT, 'setup receipt revision must be monotonic');
END;
CREATE TRIGGER IF NOT EXISTS setup_evidence_receipts_no_update
BEFORE UPDATE ON setup_evidence_receipts BEGIN
    SELECT RAISE(ABORT, 'setup evidence receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS setup_evidence_receipts_no_delete
BEFORE DELETE ON setup_evidence_receipts BEGIN
    SELECT RAISE(ABORT, 'setup evidence receipts are append-only');
END;
"""


class ReceiptError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReceiptInput(StrictModel):
    receiptId: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=96)
    evidenceDigest: str
    completedAt: datetime
    expiresAt: datetime | None = None
    expectedRevision: int = Field(ge=0)

    @field_validator("receiptId", "source", "subject")
    @classmethod
    def identity(cls, value: str) -> str:
        if not IDENTITY.fullmatch(value):
            raise ValueError("must be a bounded machine identity")
        return value

    @field_validator("evidenceDigest")
    @classmethod
    def digest(cls, value: str) -> str:
        if not DIGEST.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("completedAt", "expiresAt", mode="before")
    @classmethod
    def parse_json_datetime(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
        return value

    @field_validator("completedAt", "expiresAt")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("must include a timezone")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def ordered(self):
        if self.expiresAt is not None and self.expiresAt <= self.completedAt:
            raise ValueError("expiresAt must be later than completedAt")
        return self


class Receipt(StrictModel):
    step: ReceiptStep
    revision: int = Field(gt=0)
    receiptId: str
    source: str
    subject: str
    evidenceDigest: str
    completedAt: datetime
    expiresAt: datetime | None
    recordedAt: datetime
    state: ReceiptState


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def _payload(row: sqlite3.Row) -> tuple:
    return (
        row["source"],
        row["subject"],
        row["evidence_digest"],
        row["completed_at"],
        row["expires_at"],
    )


def _input_payload(value: ReceiptInput) -> tuple:
    return (
        value.source,
        value.subject,
        value.evidenceDigest,
        value.completedAt.timestamp(),
        value.expiresAt.timestamp() if value.expiresAt else None,
    )


def _project(row: sqlite3.Row, now: datetime) -> Receipt:
    completed = _utc(row["completed_at"])
    expires = _utc(row["expires_at"]) if row["expires_at"] is not None else None
    validity = MAX_VALIDITY[row["step"]]
    stale = (
        expires is None
        or completed > now
        or expires <= completed
        or expires - completed > validity
        or expires <= now
    )
    return Receipt(
        step=row["step"],
        revision=row["revision"],
        receiptId=row["receipt_id"],
        source=row["source"],
        subject=row["subject"],
        evidenceDigest=row["evidence_digest"],
        completedAt=completed,
        expiresAt=expires,
        recordedAt=_utc(row["recorded_at"]),
        state="stale" if stale else "valid",
    )


def current(store, step: ReceiptStep, *, now: datetime | None = None) -> Receipt | None:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM setup_evidence_receipts WHERE step=? ORDER BY revision DESC LIMIT 1",
            (step,),
        ).fetchone()
    return _project(row, observed) if row else None


def record(
    store,
    step: ReceiptStep,
    value: ReceiptInput,
    *,
    now: datetime | None = None,
) -> Receipt:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    if value.completedAt > observed:
        raise ReceiptError("future_completion", "completedAt is in the future.")
    if value.expiresAt is not None and value.expiresAt <= observed:
        raise ReceiptError("expired_evidence", "Evidence is already expired.")
    validity = MAX_VALIDITY.get(step)
    if validity is not None:
        if value.expiresAt is None:
            raise ReceiptError("expiry_required", f"{step} evidence requires expiresAt.")
        if value.expiresAt - value.completedAt > validity:
            raise ReceiptError(
                "validity_too_long",
                f"{step} evidence may be valid for at most {validity.days} days.",
            )

    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        replay = db.execute(
            "SELECT * FROM setup_evidence_receipts WHERE receipt_id=?", (value.receiptId,)
        ).fetchone()
        if replay:
            if replay["step"] == step and _payload(replay) == _input_payload(value):
                db.execute("COMMIT")
                return _project(replay, observed)
            db.execute("ROLLBACK")
            raise ReceiptError(
                "replay_conflict", "receiptId was already used for different evidence.", 409
            )

        reused = db.execute(
            "SELECT step FROM setup_evidence_receipts WHERE evidence_digest=?",
            (value.evidenceDigest,),
        ).fetchone()
        if reused:
            db.execute("ROLLBACK")
            code = "cross_step_evidence" if reused["step"] != step else "replay_conflict"
            raise ReceiptError(code, "Evidence digest was already recorded.", 409)

        revision = db.execute(
            "SELECT COALESCE(MAX(revision),0) FROM setup_evidence_receipts WHERE step=?", (step,)
        ).fetchone()[0]
        if value.expectedRevision != revision:
            db.execute("ROLLBACK")
            raise ReceiptError("revision_conflict", f"Expected revision {revision}.", 409)
        next_revision = revision + 1
        db.execute(
            "INSERT INTO setup_evidence_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                step,
                next_revision,
                value.receiptId,
                value.source,
                value.subject,
                value.evidenceDigest,
                value.completedAt.timestamp(),
                value.expiresAt.timestamp() if value.expiresAt else None,
                observed.timestamp(),
            ),
        )
        row = db.execute(
            "SELECT * FROM setup_evidence_receipts WHERE step=? AND revision=?",
            (step, next_revision),
        ).fetchone()
        db.execute("COMMIT")
    return _project(row, observed)
