"""Request and response models for cron jobs."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints, field_validator, model_validator

from pi.core.models import Strict


class Timing(Strict):
    kind: Literal["daily", "weekly", "interval"]
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    day: int = Field(ge=0, le=6)
    hours: int = Field(ge=1, le=168)


class Target(Strict):
    kind: Literal["tool", "automation"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
    publishedVersion: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    args: dict

    @field_validator("args")
    @classmethod
    def bounded(cls, value):
        if len(json.dumps(value, allow_nan=False)) > 100000:
            raise ValueError("Job inputs exceed 100000 characters.")
        return value


class AgentTarget(Strict):
    """An AI check: one turn by the job's agent in the job's own chat (see agent_checks)."""

    kind: Literal["agent"]


class Definition(Strict):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)
    agentId: str = Field(min_length=1, max_length=200)
    timing: Timing
    timeZone: str = Field(min_length=1, max_length=100)
    enabled: bool
    target: Annotated[Target | AgentTarget, Field(discriminator="kind")]
    overlap: Literal["skip"] = "skip"
    requireBudget: bool = False
    budgetAllowanceId: str | None = Field(default=None, pattern=r"^allowance_[a-f0-9]{32}$")

    @model_validator(mode="after")
    def allowance_requires_budget(self):
        if self.budgetAllowanceId and not self.requireBudget:
            raise ValueError("A recurring allowance requires budgeted execution.")
        # An AI check spends through the tools it calls, each with ToolGate's own limits.
        if self.target.kind == "agent" and self.requireBudget:
            raise ValueError("AI checks use each tool's own budget, not a job budget.")
        return self

    @field_validator("timeZone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Use an available IANA time zone.") from exc
        return value

    @field_validator("name", "instructions", "agentId")
    @classmethod
    def text(cls, value):
        if not value.strip():
            raise ValueError("Provide nonempty text.")
        return value.strip()


class Update(Strict):
    expected_revision: int = Field(ge=1)
    definition: Definition


JobId = Annotated[str, StringConstraints(pattern=r"^job_[a-f0-9]{32}$")]


RunId = Annotated[str, StringConstraints(pattern=r"^scheduled_[a-f0-9]{32}$")]


class StateChange(Strict):
    expected_revision: int = Field(ge=1)
    enabled: bool


class CheckCreate(Strict):
    """What the browser may create: an AI check, never a raw tool target."""

    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)
    agentId: str = Field(default="companion", min_length=1, max_length=200)
    hours: int = Field(ge=1, le=24)
    timeZone: str = Field(min_length=1, max_length=100)
    enabled: bool = True

    @field_validator("name", "instructions", "agentId")
    @classmethod
    def text(cls, value):
        if not value.strip():
            raise ValueError("Provide nonempty text.")
        return value.strip()

    @field_validator("timeZone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Use an available IANA time zone.") from exc
        return value


class TargetSummary(Strict):
    kind: Literal["tool", "automation"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
    publishedVersion: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    inputsConfigured: bool


class AgentTargetSummary(Strict):
    kind: Literal["agent"]
    # The check's own chat, where its turns, replies and approvals live.
    sessionId: str | None = Field(default=None, pattern=r"^ses_[a-f0-9]{16}$")


class DefinitionSummary(Strict):
    name: str = Field(min_length=1, max_length=80)
    instructionsConfigured: bool
    agentId: str = Field(min_length=1, max_length=200)
    timing: Timing
    timeZone: str = Field(min_length=1, max_length=100)
    enabled: bool
    state: Literal["enabled", "paused"]
    target: Annotated[TargetSummary | AgentTargetSummary, Field(discriminator="kind")]
    overlap: Literal["skip"]
    requireBudget: bool
    budgetAllowanceConfigured: bool


class JobView(Strict):
    schemaVersion: Literal[1] = 1
    id: JobId
    revision: int = Field(ge=1)
    definition: DefinitionSummary
    createdAt: float = Field(ge=0, allow_inf_nan=False)
    nextAt: float = Field(ge=0, allow_inf_nan=False)
    authority: Literal["none"] = "none"
    contentIncluded: Literal[False] = False
    execution: Literal["not-triggered"] = "not-triggered"


class JobCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[JobView] = Field(max_length=100)
    nextCursor: JobId | None = None


RunStatus = Literal[
    "ready",
    "awaiting_budget",
    "dispatching",
    "completed",
    "failed",
    "awaiting_approval",
    "outcome_unknown",
    "cancelled",
]


class RunView(Strict):
    schemaVersion: Literal[1] = 1
    id: RunId
    jobId: JobId
    jobRevision: int = Field(ge=1)
    scheduledAt: float = Field(ge=0, allow_inf_nan=False)
    startedAt: float = Field(ge=0, allow_inf_nan=False)
    status: RunStatus
    manual: bool
    budgetBound: bool
    receiptRecorded: bool
    outcomeCode: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,64}$")
    authority: Literal["none"] = "none"
    contentIncluded: Literal[False] = False
    execution: Literal["admitted-only", "waiting", "dispatched", "resolved", "uncertain"]


class CheckNote(Strict):
    id: str = Field(pattern=r"^note_[a-f0-9]{32}$")
    jobId: JobId
    sessionId: str = Field(pattern=r"^ses_[a-f0-9]{16}$")
    messageId: str | None = Field(default=None, pattern=r"^msg_[a-f0-9]{16}$")
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(max_length=400)
    createdAt: float = Field(ge=0, allow_inf_nan=False)


class CheckNoteCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[CheckNote] = Field(max_length=50)


class RunCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[RunView] = Field(max_length=100)
    nextCursor: RunId | None = None


class RunActionView(RunView):
    replayed: bool


class BudgetBinding(Strict):
    budget_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")
