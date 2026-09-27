"""Bounded host-CLI writes to fixed Pi owner-control operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, BinaryIO, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from pi import (
    agents,
    artifacts,
    calls,
    characters,
    collaboration,
    filesystem_reads,
    jobs,
    memory_forget,
    projects,
    proposals,
    session_settings,
    system_inventory,
    tasks,
)
from pi.browser_contract import owner_allowed, runtime_allowed

from .api import Config
from .toolgate_contract import owner_editor_allowed, owner_request_allowed

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class MutationError(ValueError):
    pass


@dataclass(frozen=True)
class Mutation:
    method: str
    path: str
    payload: dict
    authority: Literal["owner", "runtime", "toolgate-owner", "toolgate-owner-execution"] = "owner"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentCreate(StrictModel):
    operation: Literal["create"]
    configuration: agents.AgentInput


class AgentUpdate(StrictModel):
    operation: Literal["update"]
    id: str = Field(pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    expected_revision: int = Field(ge=1)
    configuration: agents.AgentInput


class AgentArchive(StrictModel):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^agent_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    archived: bool


AgentMutation = Annotated[
    AgentCreate | AgentUpdate | AgentArchive, Field(discriminator="operation")
]
AGENT_MUTATION = TypeAdapter(AgentMutation)


class ProjectCreate(StrictModel):
    operation: Literal["create"]
    fields: projects.Fields


class ProjectUpdate(StrictModel):
    operation: Literal["update"]
    id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    fields: projects.Fields


class ProjectArchive(StrictModel):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    archived: bool


class ProjectLink(StrictModel):
    operation: Literal["link", "unlink"]
    id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    reference: projects.Reference


ProjectMutation = Annotated[
    ProjectCreate | ProjectUpdate | ProjectArchive | ProjectLink,
    Field(discriminator="operation"),
]
PROJECT_MUTATION = TypeAdapter(ProjectMutation)


class TeamCreate(StrictModel):
    operation: Literal["create"]
    definition: collaboration.Team


class TeamUpdate(StrictModel):
    operation: Literal["update"]
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    definition: collaboration.Team


class TeamArchive(StrictModel):
    operation: Literal["archive"]
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)


class TeamRestore(StrictModel):
    operation: Literal["restore"]
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)


TeamMutation = Annotated[
    TeamCreate | TeamUpdate | TeamArchive | TeamRestore,
    Field(discriminator="operation"),
]
TEAM_MUTATION = TypeAdapter(TeamMutation)


class JobState(StrictModel):
    operation: Literal["state"]
    id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    enabled: bool


class JobRun(StrictModel):
    operation: Literal["run"]
    id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


JobMutation = Annotated[
    JobState | JobRun,
    Field(discriminator="operation"),
]
JOB_MUTATION = TypeAdapter(JobMutation)


class MemoryForget(StrictModel):
    operation: Literal["forget"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    memory_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
    expected_revision: int = Field(ge=1)


class ProposalDecision(StrictModel):
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


class ArtifactCreate(artifacts.Create):
    operation: Literal["create"]


class ArtifactFromMessage(artifacts.FromMessage):
    operation: Literal["from_message"]


class ArtifactAppend(artifacts.Append):
    operation: Literal["append"]
    id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")


class ArtifactRestore(artifacts.Restore):
    operation: Literal["restore"]
    id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")


class ArtifactArchive(artifacts.Archive):
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


class ApprovalDecision(StrictModel):
    operation: Literal["decide"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    status: Literal["approved", "rejected", "dismissed"]
    note: str = Field(default="", max_length=2000)


class TurnResume(StrictModel):
    operation: Literal["resume"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")


class SubmissionCancel(StrictModel):
    operation: Literal["cancel_submission"]
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


TurnMutation = Annotated[TurnResume | SubmissionCancel, Field(discriminator="operation")]
TURN_MUTATION = TypeAdapter(TurnMutation)


class FileListingRequest(filesystem_reads.Read):
    operation: Literal["request"]


class FileListingResume(StrictModel):
    operation: Literal["resume"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")


FileMutation = Annotated[FileListingRequest | FileListingResume, Field(discriminator="operation")]
FILE_MUTATION = TypeAdapter(FileMutation)


class InventoryRequest(system_inventory.Read):
    operation: Literal["request"]


class InventoryResume(StrictModel):
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


class CallStart(calls.Start):
    operation: Literal["start"]


class CallUpdate(calls.Update):
    operation: Literal["update"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


class CallInterrupt(calls.Revision):
    operation: Literal["interrupt"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


class CallEnd(calls.Revision):
    operation: Literal["end"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


class CallTurn(calls.Send):
    operation: Literal["turn"]
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")


CallMutation = Annotated[
    CallStart | CallUpdate | CallInterrupt | CallEnd | CallTurn,
    Field(discriminator="operation"),
]
CALL_MUTATION = TypeAdapter(CallMutation)


class ToolSave(StrictModel):
    operation: Literal["save"]
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    expected_revision: int = Field(ge=0)
    document: dict[str, JsonValue]

    @model_validator(mode="after")
    def matching_document(self):
        if self.document.get("id") != self.id:
            raise ValueError("Draft document identity must match the route identity.")
        return self


class ToolPublish(StrictModel):
    operation: Literal["publish"]
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    expected_revision: int = Field(ge=1)
    expected_publication_version: int = Field(ge=0)
    authorization: Literal["auto", "owner_confirmation"]


class ToolAccess(StrictModel):
    operation: Literal["access"]
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    enabled: bool


class ToolRun(StrictModel):
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
        "sessions",
        "tasks",
        "teams",
        "tools",
        "turns",
    }
)


def resources() -> tuple[str, ...]:
    return tuple(sorted(MUTATIONS))


def _resolve(resource: str, payload: dict) -> Mutation:
    if resource == "models":
        return Mutation("POST", "/models/configuration", payload)
    if resource == "agents":
        try:
            value = AGENT_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Agent apply request has an unsupported shape.") from None
        if isinstance(value, AgentCreate):
            return Mutation("POST", "/agents", value.configuration.model_dump())
        if isinstance(value, AgentUpdate):
            body = agents.UpdateAgent(
                expected_revision=value.expected_revision,
                configuration=value.configuration,
            ).model_dump()
            return Mutation("POST", f"/agents/{value.id}/update", body)
        body = agents.ArchiveAgent(
            expected_revision=value.expected_revision, archived=value.archived
        ).model_dump()
        return Mutation("POST", f"/agents/{value.id}/archive", body)
    if resource == "projects":
        try:
            value = PROJECT_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Project apply request has an unsupported shape.") from None
        if isinstance(value, ProjectCreate):
            return Mutation("POST", "/projects", value.fields.model_dump())
        if isinstance(value, ProjectUpdate):
            body = projects.Update(
                expected_revision=value.expected_revision, fields=value.fields
            ).model_dump()
            return Mutation("POST", f"/projects/{value.id}/update", body)
        if isinstance(value, ProjectArchive):
            body = projects.Archive(
                expected_revision=value.expected_revision, archived=value.archived
            ).model_dump()
            return Mutation("POST", f"/projects/{value.id}/archive", body)
        body = projects.Link(
            expected_revision=value.expected_revision, reference=value.reference
        ).model_dump()
        return Mutation("POST", f"/projects/{value.id}/{value.operation}", body)
    if resource == "teams":
        try:
            value = TEAM_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Team apply request has an unsupported shape.") from None
        if isinstance(value, TeamCreate):
            return Mutation("POST", "/collaboration/teams", value.definition.model_dump())
        if isinstance(value, TeamUpdate):
            body = collaboration.UpdateTeam(
                expected_revision=value.expected_revision, definition=value.definition
            ).model_dump()
            return Mutation("POST", f"/collaboration/teams/{value.id}/update", body)
        if isinstance(value, TeamArchive):
            body = agents.ArchiveAgent(
                expected_revision=value.expected_revision, archived=True
            ).model_dump()
            return Mutation("POST", f"/collaboration/teams/{value.id}/archive", body)
        body = collaboration.Revision(expected_revision=value.expected_revision).model_dump()
        return Mutation("POST", f"/collaboration/teams/{value.id}/restore", body)
    if resource == "jobs":
        try:
            value = JOB_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Job apply request has an unsupported shape.") from None
        if isinstance(value, JobState):
            body = jobs.StateChange(
                expected_revision=value.expected_revision, enabled=value.enabled
            ).model_dump()
            return Mutation("POST", f"/jobs/{value.id}/state", body)
        return Mutation("POST", f"/jobs/{value.id}/run", {"request_id": value.request_id})
    if resource == "memory":
        try:
            value = MemoryForget.model_validate(payload)
        except ValidationError:
            raise MutationError("Memory apply request has an unsupported shape.") from None
        body = memory_forget.Forget(
            request_id=value.request_id,
            memory_id=value.memory_id,
            expected_revision=value.expected_revision,
        ).model_dump()
        return Mutation("POST", "/memory/forget", body)
    if resource == "proposals":
        try:
            value = ProposalDecision.model_validate(payload)
        except ValidationError:
            raise MutationError("Proposal apply request has an unsupported shape.") from None
        body = proposals.Decision(decision=value.decision).model_dump()
        return Mutation("POST", f"/proposals/{value.id}/decision", body, authority="runtime")
    if resource == "tasks":
        try:
            value = TASK_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Task apply request has an unsupported shape.") from None
        body = value.model_dump(exclude={"operation", "id"})
        path = "/tasks" if isinstance(value, TaskCreate) else f"/tasks/{value.id}/{value.operation}"
        return Mutation("POST", path, body, authority="runtime")
    if resource == "artifacts":
        try:
            value = ARTIFACT_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Artifact apply request has an unsupported shape.") from None
        body = value.model_dump(exclude={"operation", "id"}, exclude_none=True)
        if isinstance(value, ArtifactCreate):
            path = "/artifacts"
        elif isinstance(value, ArtifactFromMessage):
            path = "/artifacts/from-message"
        else:
            suffix = "versions" if isinstance(value, ArtifactAppend) else value.operation
            path = f"/artifacts/{value.id}/{suffix}"
        return Mutation("POST", path, body)
    if resource == "sessions":
        try:
            value = SessionUpdate.model_validate(payload)
        except ValidationError:
            raise MutationError("Session apply request has an unsupported shape.") from None
        body = value.model_dump(exclude={"operation", "id"})
        return Mutation("POST", f"/sessions/{value.id}/settings", body)
    if resource == "approvals":
        try:
            value = ApprovalDecision.model_validate(payload)
        except ValidationError:
            raise MutationError("Approval apply request has an unsupported shape.") from None
        body = value.model_dump(exclude={"operation", "id"})
        return Mutation(
            "POST",
            f"/v2/owner/requests/{value.id}/decision",
            body,
            authority="toolgate-owner",
        )
    if resource == "turns":
        try:
            value = TURN_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Turn apply request has an unsupported shape.") from None
        if isinstance(value, TurnResume):
            return Mutation("POST", f"/turns/{value.id}/resume", {}, authority="runtime")
        return Mutation(
            "POST",
            f"/turn-submissions/{value.request_id}/cancel",
            {},
            authority="runtime",
        )
    if resource == "files":
        try:
            value = FILE_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("File listing apply request has an unsupported shape.") from None
        if isinstance(value, FileListingRequest):
            return Mutation(
                "POST",
                "/system/files/listings",
                value.model_dump(exclude={"operation"}),
            )
        return Mutation("POST", f"/system/files/listings/{value.id}/resume", {})
    if resource == "inventory":
        try:
            value = INVENTORY_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Inventory apply request has an unsupported shape.") from None
        if isinstance(value, InventoryRequest):
            return Mutation(
                "POST",
                "/system/inventory",
                value.model_dump(exclude={"operation"}),
            )
        return Mutation("POST", f"/system/inventory/{value.id}/resume", {})
    if resource == "character":
        try:
            value = CHARACTER_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Character apply request has an unsupported shape.") from None
        return Mutation(
            "POST",
            f"/characters/companion/{value.operation}",
            value.model_dump(exclude={"operation"}),
        )
    if resource == "calls":
        try:
            value = CALL_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Call apply request has an unsupported shape.") from None
        body = value.model_dump(exclude={"operation", "id"}, exclude_none=True)
        if isinstance(value, CallStart):
            path = "/calls/browser"
        else:
            suffix = "turns" if isinstance(value, CallTurn) else value.operation
            path = f"/calls/browser/{value.id}/{suffix}"
        return Mutation("POST", path, body)
    if resource == "tools":
        try:
            value = TOOL_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Tool apply request has an unsupported shape.") from None
        body = value.model_dump(exclude={"operation", "id"}, exclude_none=True)
        if isinstance(value, ToolSave):
            path = f"/v2/owner/editor-drafts/{value.id}"
            authority = "toolgate-owner"
        elif isinstance(value, ToolPublish):
            path = f"/v2/owner/editor-drafts/{value.id}/publish"
            authority = "toolgate-owner"
        else:
            suffix = "access" if isinstance(value, ToolAccess) else "runs"
            path = f"/v2/owner/editor-drafts/{value.id}/{suffix}"
            authority = "toolgate-owner-execution"
        return Mutation("POST", path, body, authority=authority)
    raise MutationError("Unknown apply resource. Choose one of: " + ", ".join(resources()))


def apply_resource(
    config: Config,
    resource: str,
    stream: BinaryIO,
    client: httpx.Client,
) -> dict:
    if resource not in MUTATIONS:
        raise MutationError("Unknown apply resource. Choose one of: " + ", ".join(resources()))
    request_limit = characters.MAX_REQUEST_BYTES if resource == "character" else MAX_REQUEST_BYTES
    response_limit = characters.MAX_REQUEST_BYTES if resource == "character" else MAX_RESPONSE_BYTES
    limit_label = "66 MiB" if resource == "character" else "2 MiB"
    raw = stream.read(request_limit + 1)
    if len(raw) > request_limit:
        raise MutationError(f"Apply request exceeded {limit_label}.")
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MutationError("Apply request is invalid JSON.") from None
    if not isinstance(payload, dict):
        raise MutationError("Apply request must be a JSON object.")
    operation = _resolve(resource, payload)
    if operation.authority == "owner":
        if not config.pi_owner_key or not owner_allowed(operation.method, operation.path):
            raise MutationError("Pi owner mutation is not configured.")
        base_url = config.pi_url
        service = "Pi"
        headers = {"X-Pi-Owner-Key": config.pi_owner_key}
    elif operation.authority == "runtime":
        if not config.pi_key or not runtime_allowed(operation.method, operation.path):
            raise MutationError("Pi runtime mutation is not configured.")
        base_url = config.pi_url
        service = "Pi"
        headers = {"X-Pi-Gateway-Key": config.pi_key}
    elif operation.authority == "toolgate-owner":
        if not config.owner_key or (
            not owner_request_allowed(operation.method, operation.path)
            and not owner_editor_allowed(operation.method, operation.path)
        ):
            raise MutationError("ToolGate owner mutation is not configured.")
        base_url = config.toolgate_url
        service = "ToolGate"
        headers = {"X-ToolGate-Owner-Key": config.owner_key}
    else:
        if (
            not config.owner_key
            or not config.toolgate_execution_key
            or not owner_editor_allowed(operation.method, operation.path, execution=True)
        ):
            raise MutationError("ToolGate editor execution is not configured.")
        base_url = config.toolgate_url
        service = "ToolGate"
        headers = {
            "X-ToolGate-Owner-Key": config.owner_key,
            "X-ToolGate-Execution-Key": config.toolgate_execution_key,
        }

    try:
        with client.stream(
            operation.method,
            base_url.rstrip("/") + operation.path,
            headers={
                "Accept": "application/json",
                **headers,
            },
            json=operation.payload,
        ) as response:
            if response.status_code not in {200, 201, 202}:
                raise MutationError(
                    f"{service} rejected the {resource} mutation (HTTP {response.status_code})."
                )
            content_type = response.headers.get("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise MutationError(f"{service} returned a non-JSON mutation response.")
            declared = response.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > response_limit):
                raise MutationError(f"{service} mutation response exceeded {limit_label}.")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > response_limit:
                    raise MutationError(f"{service} mutation response exceeded {limit_label}.")
    except MutationError:
        raise
    except (httpx.HTTPError, OSError):
        raise MutationError(f"{service} {resource} mutation is unavailable.") from None

    try:
        value = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MutationError(f"{service} returned invalid mutation JSON.") from None
    if not isinstance(value, dict):
        raise MutationError(f"{service} returned an unsupported mutation response.")
    return value
