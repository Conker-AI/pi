"""The fixed owner-control operations and the request bodies each one accepts."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from pi.agents import characters, team_models
from pi.agents import profiles as agents
from pi.calls import call_models
from pi.content import artifact_models
from pi.context import projects
from pi.conversation import settings as session_settings
from pi.core.models import Strict
from pi.host import filesystem_reads, inventory_models
from pi.memory import edits as memory_edits
from pi.schedule import tasks


class AgentCreate(Strict):
    operation: Literal["create"]
    configuration: agents.AgentInput


class AgentUpdate(Strict):
    operation: Literal["update"]
    id: str = Field(pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    expected_revision: int = Field(ge=1)
    configuration: agents.AgentInput


class AgentArchive(Strict):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^agent_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    archived: bool


AgentMutation = Annotated[
    AgentCreate | AgentUpdate | AgentArchive, Field(discriminator="operation")
]


AGENT_MUTATION = TypeAdapter(AgentMutation)


class ProjectCreate(Strict):
    operation: Literal["create"]
    fields: projects.Fields


class ProjectUpdate(Strict):
    operation: Literal["update"]
    id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    fields: projects.Fields


class ProjectArchive(Strict):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    archived: bool


class ProjectLink(Strict):
    operation: Literal["link", "unlink"]
    id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    reference: projects.Reference


ProjectMutation = Annotated[
    ProjectCreate | ProjectUpdate | ProjectArchive | ProjectLink,
    Field(discriminator="operation"),
]


PROJECT_MUTATION = TypeAdapter(ProjectMutation)


class TeamCreate(Strict):
    operation: Literal["create"]
    definition: team_models.Team


class TeamUpdate(Strict):
    operation: Literal["update"]
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    definition: team_models.Team


class TeamArchive(Strict):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)


class TeamRestore(Strict):
    operation: Literal["restore"]
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)


TeamMutation = Annotated[
    TeamCreate | TeamUpdate | TeamArchive | TeamRestore,
    Field(discriminator="operation"),
]


TEAM_MUTATION = TypeAdapter(TeamMutation)


class JobState(Strict):
    operation: Literal["state"]
    id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    enabled: bool


class JobRun(Strict):
    operation: Literal["run"]
    id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


JobMutation = Annotated[
    JobState | JobRun,
    Field(discriminator="operation"),
]


JOB_MUTATION = TypeAdapter(JobMutation)


class RequestDecision(Strict):
    """Approve or decline one of Conker's requests exactly as filed."""

    operation: Literal["approve", "decline"]
    id: str = Field(pattern=r"^creq_[a-f0-9]{32}$")


class RequestSaved(Strict):
    """A tool request the owner saved in Tools (`conker apply tools`): record which draft."""

    operation: Literal["saved"]
    id: str = Field(pattern=r"^creq_[a-f0-9]{32}$")
    draftId: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


REQUEST_MUTATION = TypeAdapter(
    Annotated[RequestDecision | RequestSaved, Field(discriminator="operation")]
)


class MemoryForget(Strict):
    operation: Literal["forget"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    memory_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
    expected_revision: int = Field(ge=1)


class MemoryRemember(Strict):
    operation: Literal["remember"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    text: str = Field(min_length=1, max_length=2000)


class MemoryCorrect(memory_edits.Apply):
    operation: Literal["correct"]


class MemoryRetry(memory_edits.Retry):
    operation: Literal["retry-correction"]
    request_id: str = Field(pattern=rf"^{memory_edits.REQUEST_ID}$")


MEMORY_MUTATION = TypeAdapter(
    Annotated[
        MemoryForget | MemoryRemember | MemoryCorrect | MemoryRetry,
        Field(discriminator="operation"),
    ]
)


class ProposalDecision(Strict):
    operation: Literal["decide"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    decision: Literal["accept", "decline", "never"]


class TaskCreate(tasks.CreateTask):
    operation: Literal["create"]


class TaskUpdate(tasks.UpdateTask):
    operation: Literal["update"]
    id: str = Field(pattern=r"^tsk_[0-9a-f]{32}$")


class TaskTransition(tasks.TransitionTask):
    operation: Literal["transition"]
    id: str = Field(pattern=r"^tsk_[0-9a-f]{32}$")


class TaskArchive(tasks.ArchiveTask):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^tsk_[0-9a-f]{32}$")


TaskMutation = Annotated[
    TaskCreate | TaskUpdate | TaskTransition | TaskArchive,
    Field(discriminator="operation"),
]


TASK_MUTATION = TypeAdapter(TaskMutation)


class ArtifactCreate(artifact_models.Create):
    operation: Literal["create"]


class ArtifactFromMessage(artifact_models.FromMessage):
    operation: Literal["from_message"]


class ArtifactAppend(artifact_models.Append):
    operation: Literal["append"]
    id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")


class ArtifactRestore(artifact_models.Restore):
    operation: Literal["restore"]
    id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")


class ArtifactArchive(artifact_models.Archive):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")


ArtifactMutation = Annotated[
    ArtifactCreate | ArtifactFromMessage | ArtifactAppend | ArtifactRestore | ArtifactArchive,
    Field(discriminator="operation"),
]


ARTIFACT_MUTATION = TypeAdapter(ArtifactMutation)


class SessionUpdate(session_settings.Update):
    operation: Literal["update"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class ApprovalDecision(Strict):
    operation: Literal["decide"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    status: Literal["approved", "rejected", "dismissed"]
    note: str = Field(default="", max_length=2000)


class TurnResume(Strict):
    operation: Literal["resume"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")


class SubmissionCancel(Strict):
    operation: Literal["cancel_submission"]
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


TurnMutation = Annotated[TurnResume | SubmissionCancel, Field(discriminator="operation")]


TURN_MUTATION = TypeAdapter(TurnMutation)


class FileListingRequest(filesystem_reads.Read):
    operation: Literal["request"]


class FileListingResume(Strict):
    operation: Literal["resume"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")


FileMutation = Annotated[FileListingRequest | FileListingResume, Field(discriminator="operation")]


FILE_MUTATION = TypeAdapter(FileMutation)


class InventoryRequest(inventory_models.Read):
    operation: Literal["request"]


class InventoryResume(Strict):
    operation: Literal["resume"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")


InventoryMutation = Annotated[InventoryRequest | InventoryResume, Field(discriminator="operation")]


INVENTORY_MUTATION = TypeAdapter(InventoryMutation)


class CharacterSave(characters.Save):
    operation: Literal["save"]


class CharacterImport(characters.Import):
    operation: Literal["import"]


class CharacterRestore(characters.Restore):
    operation: Literal["restore"]


CharacterMutation = Annotated[
    CharacterSave | CharacterImport | CharacterRestore, Field(discriminator="operation")
]


CHARACTER_MUTATION = TypeAdapter(CharacterMutation)


class CallStart(call_models.Start):
    operation: Literal["start"]


class CallUpdate(call_models.Update):
    operation: Literal["update"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


class CallInterrupt(call_models.Revision):
    operation: Literal["interrupt"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


class CallEnd(call_models.Revision):
    operation: Literal["end"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


class CallTurn(call_models.Send):
    operation: Literal["turn"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


CallMutation = Annotated[
    CallStart | CallUpdate | CallInterrupt | CallEnd | CallTurn,
    Field(discriminator="operation"),
]


CALL_MUTATION = TypeAdapter(CallMutation)


class ToolSave(Strict):
    operation: Literal["save"]
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    expected_revision: int = Field(ge=0)
    document: dict[str, JsonValue]

    @model_validator(mode="after")
    def matching_document(self):
        if self.document.get("id") != self.id:
            raise ValueError("Draft document identity must match the route identity.")
        return self


class ToolPublish(Strict):
    operation: Literal["publish"]
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    expected_revision: int = Field(ge=1)
    expected_publication_version: int = Field(ge=0)
    authorization: Literal["auto", "owner_confirmation"]


class ToolAccess(Strict):
    operation: Literal["access"]
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    enabled: bool


class ToolRun(Strict):
    operation: Literal["run"]
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_id: str = Field(pattern=r"^editor_[a-f0-9]{32}$")
    args: dict[str, JsonValue] = Field(default_factory=dict)
    approval_request_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def bounded_arguments(self):
        encoded = json.dumps(
            self.args, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 32768:
            raise ValueError("Workflow arguments exceed 32 KiB.")
        return self


ToolMutation = Annotated[
    ToolSave | ToolPublish | ToolAccess | ToolRun, Field(discriminator="operation")
]


TOOL_MUTATION = TypeAdapter(ToolMutation)


MUTATIONS = frozenset(
    {
        "agents",
        "approvals",
        "artifacts",
        "calls",
        "character",
        "files",
        "inventory",
        "jobs",
        "memory",
        "models",
        "projects",
        "proposals",
        "requests",
        "sessions",
        "tasks",
        "teams",
        "tools",
        "turns",
    }
)
