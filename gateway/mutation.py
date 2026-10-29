"""Bounded host-CLI writes to fixed Pi owner-control operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import BinaryIO, Literal

import httpx
from pydantic import (
    ValidationError,
)

from gateway.mutation_models import (
    AGENT_MUTATION,
    ARTIFACT_MUTATION,
    ASK_MUTATION,
    CALL_MUTATION,
    CHARACTER_MUTATION,
    FILE_MUTATION,
    INVENTORY_MUTATION,
    JOB_MUTATION,
    MEMORY_MUTATION,
    MUTATIONS,
    PROJECT_MUTATION,
    REQUEST_MUTATION,
    TASK_MUTATION,
    TEAM_MUTATION,
    TOOL_MUTATION,
    TURN_MUTATION,
    AgentCreate,
    AgentUpdate,
    ApprovalDecision,
    ArtifactAppend,
    ArtifactCreate,
    ArtifactFromMessage,
    AskAnswer,
    CallStart,
    CallTurn,
    FileListingRequest,
    InventoryRequest,
    JobState,
    MemoryCorrect,
    MemoryRemember,
    MemoryRetry,
    ProjectArchive,
    ProjectCreate,
    ProjectUpdate,
    ProposalDecision,
    RequestDecision,
    RequestPublished,
    RequestRetry,
    RequestSaved,
    SessionUpdate,
    TaskCreate,
    TeamArchive,
    TeamCreate,
    TeamUpdate,
    ToolAccess,
    ToolPublish,
    ToolSave,
    TurnResume,
)
from pi.agents import characters, team_models
from pi.agents import profiles as agents
from pi.api.browser_contract import owner_allowed, runtime_allowed
from pi.context import projects
from pi.memory import forget as memory_forget
from pi.schedule import job_models, proposals

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
            body = team_models.UpdateTeam(
                expected_revision=value.expected_revision, definition=value.definition
            ).model_dump()
            return Mutation("POST", f"/collaboration/teams/{value.id}/update", body)
        if isinstance(value, TeamArchive):
            body = agents.ArchiveAgent(
                expected_revision=value.expected_revision, archived=True
            ).model_dump()
            return Mutation("POST", f"/collaboration/teams/{value.id}/archive", body)
        body = team_models.Revision(expected_revision=value.expected_revision).model_dump()
        return Mutation("POST", f"/collaboration/teams/{value.id}/restore", body)
    if resource == "jobs":
        try:
            value = JOB_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Job apply request has an unsupported shape.") from None
        if isinstance(value, JobState):
            body = job_models.StateChange(
                expected_revision=value.expected_revision, enabled=value.enabled
            ).model_dump()
            return Mutation("POST", f"/jobs/{value.id}/state", body)
        return Mutation("POST", f"/jobs/{value.id}/run", {"request_id": value.request_id})
    if resource == "requests":
        try:
            value = REQUEST_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Request apply has an unsupported shape.") from None
        if isinstance(value, RequestDecision) and value.reply and value.operation != "decline":
            raise MutationError("Only a decline carries a reply.")
        if isinstance(value, RequestSaved):
            body = {"draftId": value.draftId}
        elif isinstance(value, (RequestRetry, RequestPublished)):
            body = value.model_dump(exclude={"operation", "id"}, exclude_none=True)
        else:
            body = {"reply": value.reply} if value.reply and value.operation == "decline" else {}
        return Mutation("POST", f"/change-requests/{value.id}/{value.operation}", body)
    if resource == "asks":
        try:
            value = ASK_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Answer apply request has an unsupported shape.") from None
        body = value.model_dump(exclude={"operation", "id"}) if isinstance(value, AskAnswer) else {}
        return Mutation("POST", f"/asks/{value.id}/{value.operation}", body)
    if resource == "memory":
        try:
            value = MEMORY_MUTATION.validate_python(payload)
        except ValidationError:
            raise MutationError("Memory apply request has an unsupported shape.") from None
        if isinstance(value, MemoryCorrect):
            return Mutation("POST", "/memory/corrections", value.model_dump(exclude={"operation"}))
        if isinstance(value, MemoryRemember):
            return Mutation("POST", "/memory/remember", value.model_dump(exclude={"operation"}))
        if isinstance(value, MemoryRetry):
            return Mutation(
                "POST",
                f"/memory/corrections/{value.request_id}/retry",
                {"expected_payload_hash": value.expected_payload_hash},
            )
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
            # Granting Conker's key needs that key; the owner key alone cannot name it.
            authority = "toolgate-owner-execution" if value.conkerMayUse else "toolgate-owner"
            if not value.conkerMayUse:
                body.pop("conkerMayUse")
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
    if resource == "approvals":
        # As in the Inbox: his decision continues the work once ToolGate recorded it.
        from .answers import continue_after

        continue_after(client, config, payload["id"], operation.payload or {})
    return value
