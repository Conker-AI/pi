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
    """An AI job: one turn by the job's agent in the job's own chat (see agent_jobs)."""

    kind: Literal["agent"]
    # False: only its webhook, its watch or Run now start it, never its timing.
    clock: bool = True


class ToolWatch(Strict):
    """Start an AI job when a get tool's answer changes; Pi looks every few minutes."""

    toolId: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    args: dict = Field(default_factory=dict)
    everyMinutes: int = Field(default=15, ge=5, le=1440)

    @field_validator("args")
    @classmethod
    def bounded(cls, value):
        if len(json.dumps(value, allow_nan=False)) > 10000:
            raise ValueError("Watch inputs exceed 10000 characters.")
        return value


class JobPrivacy(Strict):
    """The chat an AI job was asked for in was private, so the job's own chat is too."""

    memoryDisabled: bool = False
    harnessDisabled: bool = False


class Definition(Strict):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)
    agentId: str = Field(min_length=1, max_length=200)
    timing: Timing
    timeZone: str = Field(min_length=1, max_length=100)
    enabled: bool
    target: Annotated[Target | AgentTarget, Field(discriminator="kind")]
    # When set, the job starts when this tool has something new instead of on its timing.
    watch: ToolWatch | None = None
    overlap: Literal["skip"] = "skip"
    requireBudget: bool = False
    budgetAllowanceId: str | None = Field(default=None, pattern=r"^allowance_[a-f0-9]{32}$")
    # Who made it: the owner (by hand, or by approving Conker's request in the Inbox) or Conker on
    # its own, when his access settings let conker.schedule run without asking (see agent_jobs).
    createdBy: Literal["owner", "conker"] = "owner"
    privacy: JobPrivacy | None = None
    # Pi turned it off because the chat Conker made it in was forgotten; he can turn it back on.
    turnedOff: Literal["chat_forgotten"] | None = None
    # When the owner took this job Conker made as his own (`agent_jobs.adopt`); None otherwise.
    adoptedAt: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def allowance_requires_budget(self):
        if self.budgetAllowanceId and not self.requireBudget:
            raise ValueError("A recurring allowance requires budgeted execution.")
        if self.target.kind != "agent" and (
            self.createdBy != "owner" or self.privacy is not None or self.turnedOff is not None
        ):
            raise ValueError("Only AI jobs are made by Conker or carry a chat's privacy.")
        if self.adoptedAt is not None and (
            self.target.kind != "agent" or self.createdBy != "owner"
        ):
            raise ValueError("Only an AI job the owner took as his own records when.")
        # An AI job spends through the tools it calls, each with ToolGate's own limits.
        if self.target.kind == "agent" and self.requireBudget:
            raise ValueError("AI jobs use each tool's own budget, not a job budget.")
        if self.watch is not None and self.target.kind != "agent":
            raise ValueError("Only AI jobs can start from a tool.")
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


class Adopt(Strict):
    """The owner takes a job Conker made as his own, at the revision he looked at."""

    expected_revision: int = Field(ge=1)


class AgentJobCreate(Strict):
    """What the browser may create: an AI job with any timing, never a raw tool target."""

    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)
    agentId: str = Field(default="companion", min_length=1, max_length=200)
    timing: Timing
    timeZone: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    watch: ToolWatch | None = None
    # False: only a webhook or Run now start it (see AgentTarget.clock).
    clock: bool = True

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
    # The AI job's own chat, where its turns, replies and approvals live.
    sessionId: str | None = Field(default=None, pattern=r"^ses_[a-f0-9]{16}$")
    # Whether a webhook can start it; the secret itself is shown once, when it is made.
    webhook: bool = False
    clock: bool = True


class WatchSummary(Strict):
    toolId: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    everyMinutes: int = Field(ge=5, le=1440)
    inputsConfigured: bool
    lastCheckedAt: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    problem: Literal["needs_approval", "no_credential", "failed"] | None = None


class DefinitionSummary(Strict):
    name: str = Field(min_length=1, max_length=80)
    instructionsConfigured: bool
    # The job's own instructions in full, for the owner's job page. Only the owner's routes serve
    # this view; the gateway's runtime key cannot read /jobs. The boolean stays for older readers.
    instructions: str = Field(min_length=1, max_length=4000)
    agentId: str = Field(min_length=1, max_length=200)
    # The agent's name from its profile, to show instead of its id; None when Pi has no such agent.
    agentName: str | None = Field(default=None, min_length=1, max_length=80)
    timing: Timing
    timeZone: str = Field(min_length=1, max_length=100)
    enabled: bool
    state: Literal["enabled", "paused"]
    target: Annotated[TargetSummary | AgentTargetSummary, Field(discriminator="kind")]
    watch: WatchSummary | None = None
    overlap: Literal["skip"]
    requireBudget: bool
    budgetAllowanceConfigured: bool
    createdBy: Literal["owner", "conker"] = "owner"
    turnedOff: Literal["chat_forgotten"] | None = None
    # Conker made it and the owner took it as his own at this time; None for every other job.
    adoptedAt: float | None = Field(default=None, ge=0, allow_inf_nan=False)


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


class AgentJobNote(Strict):
    id: str = Field(pattern=r"^note_[a-f0-9]{32}$")
    jobId: JobId
    sessionId: str = Field(pattern=r"^ses_[a-f0-9]{16}$")
    messageId: str | None = Field(default=None, pattern=r"^msg_[a-f0-9]{16}$")
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(max_length=400)
    createdAt: float = Field(ge=0, allow_inf_nan=False)


class HookIssued(Strict):
    jobId: JobId
    token: str = Field(pattern=r"^hook_[A-Za-z0-9_-]{43}$")
    header: Literal["X-Conker-Hook-Token"]
    path: str = Field(pattern=r"^/hooks/job_[a-f0-9]{32}$")


class HookReceived(Strict):
    started: bool
    runId: RunId | None = None
    reason: Literal["busy", "paused"] | None = None


class AgentJobNoteCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[AgentJobNote] = Field(max_length=50)


class RunCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[RunView] = Field(max_length=100)
    nextCursor: RunId | None = None


class RunActionView(RunView):
    replayed: bool


class BudgetBinding(Strict):
    budget_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")
