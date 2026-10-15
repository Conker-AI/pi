"""Durable first-run backup policy owned by Pi and executed by the host."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,127}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_protection_policies (
    revision          INTEGER PRIMARY KEY CHECK(revision > 0),
    request_id        TEXT NOT NULL UNIQUE,
    destination       TEXT NOT NULL,
    retention_copies  INTEGER NOT NULL CHECK(retention_copies BETWEEN 2 AND 64),
    policy_digest     TEXT NOT NULL,
    recorded_at       REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS setup_protection_policies_revision_sequence
BEFORE INSERT ON setup_protection_policies
WHEN NEW.revision != COALESCE((SELECT MAX(revision) + 1 FROM setup_protection_policies), 1)
BEGIN
    SELECT RAISE(ABORT, 'setup protection policy revision must be monotonic');
END;
CREATE TRIGGER IF NOT EXISTS setup_protection_policies_no_update
BEFORE UPDATE ON setup_protection_policies BEGIN
    SELECT RAISE(ABORT, 'setup protection policies are append-only');
END;
CREATE TRIGGER IF NOT EXISTS setup_protection_policies_no_delete
BEFORE DELETE ON setup_protection_policies BEGIN
    SELECT RAISE(ABORT, 'setup protection policies are append-only');
END;
"""


class ProtectionError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PolicyInput(StrictModel):
    requestId: str = Field(min_length=1, max_length=128)
    destination: str = Field(min_length=2, max_length=1024)
    retentionCopies: int = Field(ge=2, le=64)
    expectedRevision: int = Field(ge=0)

    @field_validator("requestId")
    @classmethod
    def identity(cls, value: str) -> str:
        if not IDENTITY.fullmatch(value):
            raise ValueError("must be a bounded machine identity")
        return value

    @field_validator("destination")
    @classmethod
    def host_path(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("must not contain control characters")
        path = PurePosixPath(value)
        if not path.is_absolute() or path == PurePosixPath("/"):
            raise ValueError("must be an absolute host path below filesystem root")
        if str(path) != value or ".." in path.parts:
            raise ValueError("must be a normalized absolute host path")
        return value


class Policy(StrictModel):
    schemaVersion: Literal[1] = 1
    revision: int = Field(ge=0)
    requestId: str | None
    destinationKind: Literal["mounted_off_machine"] = "mounted_off_machine"
    destination: str | None
    retentionCopies: int | None
    policyDigest: str | None
    recordedAt: datetime | None


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _digest(destination: str, retention_copies: int) -> str:
    payload = json.dumps(
        {
            "destination": destination,
            "destinationKind": "mounted_off_machine",
            "retentionCopies": retention_copies,
            "schemaVersion": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _project(row: sqlite3.Row) -> Policy:
    return Policy(
        revision=row["revision"],
        requestId=row["request_id"],
        destination=row["destination"],
        retentionCopies=row["retention_copies"],
        policyDigest=row["policy_digest"],
        recordedAt=datetime.fromtimestamp(row["recorded_at"], UTC),
    )


def current(store) -> Policy:
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM setup_protection_policies ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    return (
        _project(row)
        if row
        else Policy(
            revision=0,
            requestId=None,
            destination=None,
            retentionCopies=None,
            policyDigest=None,
            recordedAt=None,
        )
    )


def save(store, value: PolicyInput, *, now: datetime | None = None) -> Policy:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    digest = _digest(value.destination, value.retentionCopies)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        replay = db.execute(
            "SELECT * FROM setup_protection_policies WHERE request_id=?", (value.requestId,)
        ).fetchone()
        if replay:
            if (
                replay["destination"] == value.destination
                and replay["retention_copies"] == value.retentionCopies
            ):
                db.execute("COMMIT")
                return _project(replay)
            db.execute("ROLLBACK")
            raise ProtectionError(
                "replay_conflict", "requestId was already used for a different policy.", 409
            )
        revision = db.execute(
            "SELECT COALESCE(MAX(revision),0) FROM setup_protection_policies"
        ).fetchone()[0]
        if value.expectedRevision != revision:
            db.execute("ROLLBACK")
            raise ProtectionError("revision_conflict", f"Expected revision {revision}.", 409)
        next_revision = revision + 1
        db.execute(
            "INSERT INTO setup_protection_policies VALUES (?,?,?,?,?,?)",
            (
                next_revision,
                value.requestId,
                value.destination,
                value.retentionCopies,
                digest,
                observed.timestamp(),
            ),
        )
        row = db.execute(
            "SELECT * FROM setup_protection_policies WHERE revision=?", (next_revision,)
        ).fetchone()
        db.execute("COMMIT")
    return _project(row)


def receipt_subject(policy: Policy) -> str | None:
    if policy.revision == 0 or policy.policyDigest is None:
        return None
    return f"installation.repository.protection.{policy.revision}.{policy.policyDigest[:16]}"
