"""Request and response models for agent teams."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from pi.agents import profiles as agents
from pi.core.models import Strict


class Budget(Strict):
    maxTurns: int = Field(ge=1, le=200)
    maxTokens: int = Field(ge=1, le=1000000)
    maxCostCents: int = Field(ge=0, le=1000000)


class TeamBudget(Budget):
    maxHandoffs: int = Field(ge=0, le=100)


class Context(Strict):
    mode: Literal["task_only", "selected"]
    sourceIds: list[str] = Field(max_length=100)

    @field_validator("sourceIds")
    @classmethod
    def ids(cls, values):
        return agents.references(values)

    @model_validator(mode="after")
    def selected(self):
        if (self.mode == "selected") != bool(self.sourceIds):
            raise ValueError("Only selected context requires explicit source IDs.")
        return self


class Role(Strict):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    agentId: str = Field(pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    instructions: str = Field(min_length=1, max_length=8000)
    toolIds: list[str] = Field(max_length=100)
    memory: agents.MemorySelection
    context: Context
    budget: Budget

    @field_validator("name", "instructions")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)

    @field_validator("agentId")
    @classmethod
    def identity(cls, value):
        agents.references([value])
        return value

    @field_validator("toolIds")
    @classmethod
    def tools(cls, values):
        return agents.references(values)

    @field_validator("memory")
    @classmethod
    def memory_limit(cls, value):
        if value.scope == "owner":
            raise ValueError("Across-chat owner memory is not available to team roles.")
        if len(value.memoryIds) > 100:
            raise ValueError("Use at most 100 selected memory records.")
        return value


class Handoff(Strict):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    fromRoleId: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    toRoleId: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    condition: str = Field(min_length=1, max_length=1000)
    payload: Literal["result_only", "result_and_citations"]
    maxTransfers: int = Field(ge=1, le=100)

    @field_validator("condition")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)


class Team(Strict):
    name: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2000)
    roles: list[Role] = Field(min_length=1, max_length=12)
    handoffs: list[Handoff] = Field(max_length=30)
    budget: TeamBudget

    @field_validator("name", "objective")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)

    @model_validator(mode="after")
    def graph(self):
        ids = [role.id for role in self.roles]
        for values in (
            ids,
            [role.name.casefold() for role in self.roles],
            [edge.id for edge in self.handoffs],
            [(edge.fromRoleId, edge.toRoleId) for edge in self.handoffs],
        ):
            if len(values) != len(set(values)):
                raise ValueError("Role IDs/names, handoff IDs and directed pairs must be unique.")
        for edge in self.handoffs:
            if (
                edge.fromRoleId == edge.toRoleId
                or edge.fromRoleId not in ids
                or edge.toRoleId not in ids
            ):
                raise ValueError("Handoffs connect two different existing roles.")
        if sum(edge.maxTransfers for edge in self.handoffs) > self.budget.maxHandoffs:
            raise ValueError("Handoff allocations exceed the team budget.")
        for field in Budget.model_fields:
            if sum(getattr(role.budget, field) for role in self.roles) > getattr(
                self.budget, field
            ):
                raise ValueError("Role allocations exceed the team " + field + " budget.")
        return self


class Template(Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    agent: agents.AgentInput

    @field_validator("name", "description")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)

    @field_validator("agent")
    @classmethod
    def limits(cls, value):
        if len(value.toolIds) > 100 or len(value.memory.memoryIds) > 100:
            raise ValueError("Use at most 100 tool or memory selections.")
        return value


class Revision(Strict):
    expected_revision: int = Field(ge=1)


class UpdateTemplate(Revision):
    definition: Template


class UpdateTeam(Revision):
    definition: Team


class TeamSummary(Strict):
    schemaVersion: Literal[1] = 1
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    archived_at: float | None = Field(ge=0, allow_inf_nan=False)
    created_at: float = Field(ge=0, allow_inf_nan=False)
    updated_at: float = Field(ge=0, allow_inf_nan=False)
    authority: Literal["none"] = "none"
    execution: Literal["configuration-only"] = "configuration-only"
    contentIncluded: Literal[False] = False
    reference_validation: Literal["external-references-unverified"] = (
        "external-references-unverified"
    )


class AgentReference(Strict):
    roleId: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    agentId: str = Field(pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    revision: int = Field(ge=1)


class TeamView(TeamSummary):
    definition: Team
    contentIncluded: Literal[True] = True
    agentReferences: list[AgentReference] = Field(max_length=12)
    agentReferenceState: Literal["available", "unavailable", "selection-conflict"]


class TeamCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[TeamSummary] = Field(max_length=100)
    nextCursor: str | None = Field(default=None, pattern=r"^team_[0-9a-f]{32}$")


class TeamRevision(TeamSummary):
    definition: Team
    contentIncluded: Literal[True] = True
    historical: Literal[True] = True


class TeamHistory(Strict):
    schemaVersion: Literal[1] = 1
    results: list[TeamSummary] = Field(max_length=100)
    nextRevision: int | None = Field(default=None, ge=1)


class Instantiate(Revision):
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    # Whole fields only; fully merged configuration is strictly validated below.
    overrides: dict = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def allowed_fields(cls, value):
        if set(value) - (set(agents.AgentInput.model_fields) - {"name"}):
            raise ValueError("Only agent fields other than name may be overridden.")
        return value


class Start(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    expected_revision: int = Field(ge=1)
    source_session_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8000)
    budgetMode: Literal["observed-stop"]
    acknowledgeCurrentCallMayOvershoot: Literal[True]


class Step(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    expected_revision: int = Field(ge=1)
    roleId: str = Field(min_length=1, max_length=64)
    handoffId: str | None = Field(default=None, min_length=1, max_length=64)
    conditionReviewed: bool = False


class Finish(Strict):
    expected_revision: int = Field(ge=1)
    state: Literal["completed", "cancelled"]
