"""Request bodies that Pi's schedule packages validate and the owner gateway checks before relaying."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from pi.core.models import Strict

Status = Literal["planned", "in_progress", "blocked", "completed", "cancelled"]


class Decision(Strict):
    decision: Literal["accept", "decline", "never"]


class StateChange(Strict):
    expected_revision: int = Field(ge=1)
    enabled: bool


class TaskFields(Strict):
    outcome: str = Field(min_length=1, max_length=1000)
    criteria: list[str] = Field(min_length=1, max_length=20)
    parent_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("outcome")
    @classmethod
    def outcome_text(cls, value):
        if not value.strip():
            raise ValueError("Provide an outcome.")
        return value.strip()

    @field_validator("criteria")
    @classmethod
    def criterion_text(cls, values):
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("Each criterion needs 1-500 characters.")
        values = [value.strip() for value in values]
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("Criteria must be distinct.")
        return values

    @field_validator("run_ids")
    @classmethod
    def distinct_runs(cls, values):
        if len(set(values)) != len(values) or any(
            not value or len(value) > 128 for value in values
        ):
            raise ValueError("Link distinct existing run identities.")
        return values


class CreateTask(TaskFields):
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    session_id: str = Field(min_length=1, max_length=128)


class UpdateTask(TaskFields):
    expected_revision: int = Field(ge=1)


class TransitionTask(Strict):
    expected_revision: int = Field(ge=1)
    status: Status
    note: str = Field(min_length=1, max_length=2000)
    completed_criterion_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("note")
    @classmethod
    def note_text(cls, value):
        if not value.strip():
            raise ValueError("Explain this status change.")
        return value.strip()


class ArchiveTask(Strict):
    expected_revision: int = Field(ge=1)
    archived: bool
