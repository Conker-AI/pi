"""Bounded host-CLI writes to fixed Pi owner-control operations.

A decision on a ToolGate request (`approvals`) asks for exactly what the Inbox asks for
(`approval_levels`): a yes at level 3, or at a level that cannot be read, needs the owner's
password, which comes with the decision on stdin (`"password"`); without it nothing is decided and
the command exits with `PASSWORD_REQUIRED_EXIT`, so the CLI can ask for it and send the same
decision once more.
"""

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
    ToolRunPublished,
    ToolSave,
    TurnResume,
)
from pi.agents import characters, team_models
from pi.agents import profiles as agents
from pi.api.browser_contract import owner_allowed, runtime_allowed
from pi.core import project_bodies, schedule_bodies
from pi.memory import forget as memory_forget

from . import approval_levels
from .api import Config
from .store import AuthError, AuthStore
from .toolgate_contract import owner_editor_allowed, owner_request_allowed

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class MutationError(ValueError):
    pass


class PasswordRequired(MutationError):
    """A yes the Inbox would ask the password for, sent without it: nothing was decided."""


# What `python -m gateway apply approvals` exits with on PasswordRequired.
PASSWORD_REQUIRED_EXIT = 3


# After the write was sent, or once the service answered 2xx, it may already have been made.
MAYBE_DONE = " It may already have been made; check its current state before trying again."


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
            body = project_bodies.Update(
                expected_revision=value.expected_revision, fields=value.fields
            ).model_dump()
            return Mutation("POST", f"/projects/{value.id}/update", body)
        if isinstance(value, ProjectArchive):
            body = project_bodies.Archive(
                expected_revision=value.expected_revision, archived=value.archived
            ).model_dump()
            return Mutation("POST", f"/projects/{value.id}/archive", body)
        body = project_bodies.Link(
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
            body = schedule_bodies.StateChange(
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
        body = schedule_bodies.Decision(decision=value.decision).model_dump()
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
        elif isinstance(value, ToolRunPublished):
            path = f"/v2/owner/tools/{value.id}/runs"
            authority = "toolgate-owner-execution"
        else:
            suffix = "access" if isinstance(value, ToolAccess) else "runs"
            path = f"/v2/owner/editor-drafts/{value.id}/{suffix}"
            authority = "toolgate-owner-execution"
        return Mutation("POST", path, body, authority=authority)
    raise MutationError("Unknown apply resource. Choose one of: " + ", ".join(resources()))


def _decision_level(config: Config, client: httpx.Client, identity: str) -> int:
    """The request's level as the Inbox reads it (owner_routes.decision_level): ToolGate fixed it
    when the request was made; anything unreadable is serious, so a doubt asks for the password."""
    try:
        response = client.get(
            config.toolgate_url.rstrip("/") + f"/v2/owner/requests/{identity}",
            headers={"Accept": "application/json", "X-ToolGate-Owner-Key": config.owner_key},
            timeout=10,
        )
        view = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, OSError, ValueError):
        return approval_levels.SERIOUS
    return approval_levels.from_view(identity, response.status_code, view)


def _admit_decision(
    config: Config, client: httpx.Client, auth: AuthStore | None, decision: dict, password
) -> None:
    """The Inbox's rule (approval_levels.needs_password), before anything is decided. A no or a
    dismissal runs nothing, so its level is not even read. A password sent is always checked."""
    serious = decision["status"] == "approved" and approval_levels.needs_password(
        decision["status"], _decision_level(config, client, decision["id"])
    )
    if password is None:
        if serious:
            raise PasswordRequired(
                "This yes asks for your password, as the Inbox does. Nothing was decided."
            )
        return
    if not isinstance(password, str) or not password or auth is None:
        raise MutationError("The password could not be checked. Nothing was decided.")
    try:
        auth.host_password(password)
    except AuthError as exc:
        raise MutationError(f"{exc} Nothing was decided.") from None


def apply_resource(
    config: Config,
    resource: str,
    stream: BinaryIO,
    client: httpx.Client,
    auth: AuthStore | None = None,
) -> dict:
    """One host-CLI write. `auth` checks the owner's password for a decision that needs it."""
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
    # Only a decision may carry the owner's password; anywhere else it is an unsupported field.
    password = payload.pop("password", None) if resource == "approvals" else None
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
    if resource == "approvals":
        _admit_decision(config, client, auth, payload, password)

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
                raise MutationError(
                    f"{service} returned a non-JSON mutation response." + MAYBE_DONE
                )
            declared = response.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > response_limit):
                raise MutationError(
                    f"{service} mutation response exceeded {limit_label}." + MAYBE_DONE
                )
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > response_limit:
                    raise MutationError(
                        f"{service} mutation response exceeded {limit_label}." + MAYBE_DONE
                    )
    except MutationError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Never reached the service: nothing was written.
        raise MutationError(f"{service} {resource} mutation is unavailable.") from None
    except (httpx.HTTPError, OSError):
        raise MutationError(
            f"{service} {resource} mutation got no complete answer." + MAYBE_DONE
        ) from None

    try:
        value = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MutationError(f"{service} returned invalid mutation JSON." + MAYBE_DONE) from None
    if not isinstance(value, dict):
        raise MutationError(f"{service} returned an unsupported mutation response." + MAYBE_DONE)
    if resource == "approvals":
        # As in the Inbox: his decision continues the work once ToolGate recorded it.
        from .answers import continue_after

        continue_after(client, config, payload["id"], operation.payload or {})
    return value
