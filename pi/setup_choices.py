"""Append-only owner choices for optional first-run capabilities."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ChoiceStep = Literal["companion", "memory", "capabilities"]
ChoiceValue = Literal["undecided", "accept", "include", "skip"]

STEPS = ("memory", "capabilities")
IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,127}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_optional_choices (
    step         TEXT NOT NULL CHECK(step IN ('memory','capabilities')),
    revision     INTEGER NOT NULL CHECK(revision > 0),
    request_id   TEXT NOT NULL UNIQUE,
    choice       TEXT NOT NULL CHECK(choice IN ('include','skip')),
    recorded_at  REAL NOT NULL,
    PRIMARY KEY (step, revision)
);
CREATE INDEX IF NOT EXISTS setup_optional_choices_latest
    ON setup_optional_choices(step, revision DESC);
CREATE TRIGGER IF NOT EXISTS setup_optional_choices_revision_sequence
BEFORE INSERT ON setup_optional_choices
WHEN NEW.revision != COALESCE(
    (SELECT MAX(revision) + 1 FROM setup_optional_choices WHERE step=NEW.step), 1
)
BEGIN
    SELECT RAISE(ABORT, 'setup choice revision must be monotonic');
END;
CREATE TRIGGER IF NOT EXISTS setup_optional_choices_no_update
BEFORE UPDATE ON setup_optional_choices BEGIN
    SELECT RAISE(ABORT, 'setup choices are append-only');
END;
CREATE TRIGGER IF NOT EXISTS setup_optional_choices_no_delete
BEFORE DELETE ON setup_optional_choices BEGIN
    SELECT RAISE(ABORT, 'setup choices are append-only');
END;
"""

COMPANION_SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_companion_choices (
    step         TEXT NOT NULL CHECK(step='companion'),
    revision     INTEGER NOT NULL CHECK(revision > 0),
    request_id   TEXT NOT NULL UNIQUE,
    choice       TEXT NOT NULL CHECK(choice='accept'),
    recorded_at  REAL NOT NULL,
    PRIMARY KEY (step, revision)
);
CREATE TRIGGER IF NOT EXISTS setup_companion_choices_revision_sequence
BEFORE INSERT ON setup_companion_choices
WHEN NEW.revision != COALESCE(
    (SELECT MAX(revision) + 1 FROM setup_companion_choices), 1
)
BEGIN
    SELECT RAISE(ABORT, 'setup companion choice revision must be monotonic');
END;
CREATE TRIGGER IF NOT EXISTS setup_companion_choices_no_update
BEFORE UPDATE ON setup_companion_choices BEGIN
    SELECT RAISE(ABORT, 'setup companion choices are append-only');
END;
CREATE TRIGGER IF NOT EXISTS setup_companion_choices_no_delete
BEFORE DELETE ON setup_companion_choices BEGIN
    SELECT RAISE(ABORT, 'setup companion choices are append-only');
END;
"""


class ChoiceError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChoiceInput(StrictModel):
    requestId: str = Field(min_length=1, max_length=128)
    choice: Literal["accept", "include", "skip"]
    expectedRevision: int = Field(ge=0)

    @field_validator("requestId")
    @classmethod
    def identity(cls, value: str) -> str:
        if not IDENTITY.fullmatch(value):
            raise ValueError("must be a bounded machine identity")
        return value


class Choice(StrictModel):
    step: ChoiceStep
    revision: int = Field(ge=0)
    requestId: str | None
    choice: ChoiceValue
    recordedAt: datetime | None


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.executescript(COMPANION_SCHEMA)


def _project(row: sqlite3.Row) -> Choice:
    return Choice(
        step=row["step"],
        revision=row["revision"],
        requestId=row["request_id"],
        choice=row["choice"],
        recordedAt=datetime.fromtimestamp(row["recorded_at"], UTC),
    )


def _table(step: ChoiceStep) -> str:
    return "setup_companion_choices" if step == "companion" else "setup_optional_choices"


def current(store, step: ChoiceStep) -> Choice:
    table = _table(step)
    with store._connect() as db:
        row = db.execute(
            f"SELECT * FROM {table} WHERE step=? ORDER BY revision DESC LIMIT 1",
            (step,),
        ).fetchone()
    return _project(row) if row else Choice(
        step=step, revision=0, requestId=None, choice="undecided", recordedAt=None
    )


def record(
    store,
    step: ChoiceStep,
    value: ChoiceInput,
    *,
    now: datetime | None = None,
) -> Choice:
    if (step == "companion") != (value.choice == "accept"):
        raise ChoiceError(
            "invalid_choice",
            "Companion accepts its current default; optional services use include or skip.",
        )
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    table = _table(step)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        replay = None
        for candidate in ("setup_optional_choices", "setup_companion_choices"):
            replay = db.execute(
                f"SELECT * FROM {candidate} WHERE request_id=?", (value.requestId,)
            ).fetchone()
            if replay:
                break
        if replay:
            if replay["step"] == step and replay["choice"] == value.choice:
                db.execute("COMMIT")
                return _project(replay)
            db.execute("ROLLBACK")
            raise ChoiceError(
                "replay_conflict", "requestId was already used for a different choice.", 409
            )

        revision = db.execute(
            f"SELECT COALESCE(MAX(revision),0) FROM {table} WHERE step=?",
            (step,),
        ).fetchone()[0]
        if value.expectedRevision != revision:
            db.execute("ROLLBACK")
            raise ChoiceError("revision_conflict", f"Expected revision {revision}.", 409)
        next_revision = revision + 1
        db.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            (step, next_revision, value.requestId, value.choice, observed.timestamp()),
        )
        row = db.execute(
            f"SELECT * FROM {table} WHERE step=? AND revision=?",
            (step, next_revision),
        ).fetchone()
        db.execute("COMMIT")
    return _project(row)
