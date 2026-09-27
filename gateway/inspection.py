"""Bounded host-CLI inspection of fixed Pi control-plane resources."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from pi import characters
from pi.browser_contract import owner_allowed, runtime_allowed

from .api import Config
from .toolgate_contract import owner_editor_allowed, owner_request_allowed

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class InspectionError(ValueError):
    pass


@dataclass(frozen=True)
class Inspection:
    authority: str
    path: str
    query: tuple[tuple[str, str], ...] = ()


INSPECTIONS = {
    "agents": Inspection("owner", "/agents"),
    "approvals": Inspection("toolgate-owner", "/v2/owner/requests"),
    "artifacts": Inspection("owner", "/artifacts"),
    "boundaries": Inspection("owner", "/setup/boundaries"),
    "character": Inspection("owner", "/characters/companion"),
    "character-export": Inspection("owner", "/characters/companion/export"),
    "character-history": Inspection("owner", "/characters/companion/history"),
    "call-capabilities": Inspection("owner", "/calls/browser/capabilities"),
    "file-roots": Inspection("owner", "/system/files/roots"),
    "inventory-containers": Inspection("owner", "/system/inventory/configured/containers"),
    "inventory-services": Inspection("owner", "/system/inventory/configured/services"),
    "jobs": Inspection("owner", "/jobs"),
    "memory": Inspection("owner", "/memory/objects"),
    "models": Inspection("owner", "/models/configuration"),
    "projects": Inspection("owner", "/projects"),
    "setup": Inspection("owner", "/setup/status"),
    "teams": Inspection("owner", "/collaboration/teams"),
    "proposals": Inspection("runtime", "/proposals"),
    "runs": Inspection("runtime", "/runs"),
    "sessions": Inspection("runtime", "/sessions"),
    "tasks": Inspection("runtime", "/tasks"),
    "tool-capabilities": Inspection(
        "toolgate-owner",
        "/v2/owner/editor-capabilities",
        (("kind", "tool"), ("limit", "50")),
    ),
    "tool-drafts": Inspection("toolgate-owner", "/v2/owner/editor-drafts", (("limit", "50"),)),
    "tools": Inspection("runtime", "/tools"),
    "workflow-capabilities": Inspection(
        "toolgate-owner",
        "/v2/owner/editor-capabilities",
        (("kind", "workflow"), ("limit", "50")),
    ),
}
SESSION_SETTINGS = re.compile(r"^session-settings:([A-Za-z0-9_-]{1,128})$")
APPROVAL = re.compile(r"^approval:([A-Za-z0-9_-]{1,128})$")
SUBMISSION = re.compile(r"^submission:([A-Za-z0-9_-]{16,128})$")
FILE_LISTING = re.compile(r"^file-listing:([A-Za-z0-9_-]{16,100})$")
INVENTORY = re.compile(r"^inventory:([A-Za-z0-9_-]{16,100})$")
CALL = re.compile(r"^call:(call_[a-f0-9]{32})$")
ACTIVE_CALL = re.compile(r"^active-call:([A-Za-z0-9_-]{1,200})$")
TOOL_DRAFT = re.compile(r"^tool-draft:([A-Za-z][A-Za-z0-9_-]{0,63})$")
TOOL_VALIDATION = re.compile(r"^tool-validation:([A-Za-z][A-Za-z0-9_-]{0,63})$")
TOOL_PUBLICATIONS = re.compile(r"^tool-publications:([A-Za-z][A-Za-z0-9_-]{0,63})$")
TOOL_RUNS = re.compile(r"^tool-runs:([A-Za-z][A-Za-z0-9_-]{0,63})$")
TOOL_ACCESS = re.compile(
    r"^tool-access:([A-Za-z][A-Za-z0-9_-]{0,63}):([1-9][0-9]{0,8}):([a-f0-9]{64})$"
)


def resources() -> tuple[str, ...]:
    return tuple(sorted(INSPECTIONS))


def inspect_resource(config: Config, resource: str, client: httpx.Client) -> dict:
    operation = INSPECTIONS.get(resource)
    if operation is None:
        selected = SESSION_SETTINGS.fullmatch(resource)
        if selected:
            operation = Inspection("owner", f"/sessions/{selected.group(1)}/settings")
    if operation is None:
        selected = APPROVAL.fullmatch(resource)
        if selected:
            operation = Inspection("toolgate-owner", f"/v2/owner/requests/{selected.group(1)}")
    if operation is None:
        selected = SUBMISSION.fullmatch(resource)
        if selected:
            operation = Inspection("runtime", f"/turn-submissions/{selected.group(1)}")
    if operation is None:
        selected = FILE_LISTING.fullmatch(resource)
        if selected:
            operation = Inspection("owner", f"/system/files/listings/{selected.group(1)}")
    if operation is None:
        selected = INVENTORY.fullmatch(resource)
        if selected:
            operation = Inspection("owner", f"/system/inventory/{selected.group(1)}")
    if operation is None:
        selected = CALL.fullmatch(resource)
        if selected:
            operation = Inspection("owner", f"/calls/browser/{selected.group(1)}")
    if operation is None:
        selected = ACTIVE_CALL.fullmatch(resource)
        if selected:
            operation = Inspection("owner", f"/calls/browser/active/{selected.group(1)}")
    if operation is None:
        selected = TOOL_DRAFT.fullmatch(resource)
        if selected:
            operation = Inspection("toolgate-owner", f"/v2/owner/editor-drafts/{selected.group(1)}")
    if operation is None:
        selected = TOOL_VALIDATION.fullmatch(resource)
        if selected:
            operation = Inspection(
                "toolgate-owner",
                f"/v2/owner/editor-drafts/{selected.group(1)}/validation",
            )
    if operation is None:
        selected = TOOL_PUBLICATIONS.fullmatch(resource)
        if selected:
            operation = Inspection(
                "toolgate-owner",
                f"/v2/owner/editor-drafts/{selected.group(1)}/publications",
            )
    if operation is None:
        selected = TOOL_RUNS.fullmatch(resource)
        if selected:
            operation = Inspection(
                "toolgate-owner-execution",
                f"/v2/owner/editor-drafts/{selected.group(1)}/runs",
            )
    if operation is None:
        selected = TOOL_ACCESS.fullmatch(resource)
        if selected:
            operation = Inspection(
                "toolgate-owner-execution",
                f"/v2/owner/editor-drafts/{selected.group(1)}/access",
                (("version", selected.group(2)), ("digest", selected.group(3))),
            )
    if operation is None:
        raise InspectionError(
            "Unknown inspection resource. Choose one of: "
            + ", ".join(resources())
            + ", active-call:ID, approval:ID, call:ID, file-listing:ID, inventory:ID, session-settings:ID, submission:ID, tool-access:ID:VERSION:DIGEST, tool-draft:ID, tool-publications:ID, tool-runs:ID, tool-validation:ID"
        )
    if operation.authority == "owner":
        if not config.pi_owner_key or not owner_allowed("GET", operation.path):
            raise InspectionError("Pi owner inspection is not configured.")
        header, credential = "X-Pi-Owner-Key", config.pi_owner_key
        base_url = config.pi_url
        service = "Pi"
    elif operation.authority == "runtime":
        if not config.pi_key or not runtime_allowed("GET", operation.path):
            raise InspectionError("Pi runtime inspection is not configured.")
        header, credential = "X-Pi-Gateway-Key", config.pi_key
        base_url = config.pi_url
        service = "Pi"
    elif operation.authority == "toolgate-owner":
        if not config.owner_key or not (
            owner_request_allowed("GET", operation.path)
            or owner_editor_allowed("GET", operation.path)
        ):
            raise InspectionError("ToolGate owner inspection is not configured.")
        header, credential = "X-ToolGate-Owner-Key", config.owner_key
        base_url = config.toolgate_url
        service = "ToolGate"
        headers = {header: credential}
    else:
        if (
            not config.owner_key
            or not config.toolgate_execution_key
            or not owner_editor_allowed("GET", operation.path, execution=True)
        ):
            raise InspectionError("ToolGate editor execution is not configured.")
        base_url = config.toolgate_url
        service = "ToolGate"
        headers = {
            "X-ToolGate-Owner-Key": config.owner_key,
            "X-ToolGate-Execution-Key": config.toolgate_execution_key,
        }

    if operation.authority in {"owner", "runtime"}:
        headers = {header: credential}

    url = base_url.rstrip("/") + operation.path
    response_limit = (
        characters.MAX_REQUEST_BYTES
        if operation.path.startswith("/characters/companion")
        else MAX_RESPONSE_BYTES
    )
    limit_label = "66 MiB" if response_limit == characters.MAX_REQUEST_BYTES else "8 MiB"
    try:
        with client.stream(
            "GET",
            url,
            headers={"Accept": "application/json", **headers},
            params=dict(operation.query),
        ) as response:
            if response.status_code != 200:
                raise InspectionError(
                    f"{service} rejected {resource} inspection (HTTP {response.status_code})."
                )
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise InspectionError(f"{service} returned a non-JSON inspection response.")
            declared = response.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > response_limit):
                raise InspectionError(f"{service} inspection response exceeded {limit_label}.")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > response_limit:
                    raise InspectionError(f"{service} inspection response exceeded {limit_label}.")
    except InspectionError:
        raise
    except (httpx.HTTPError, OSError):
        raise InspectionError(f"{service} {resource} inspection is unavailable.") from None

    try:
        value = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InspectionError(f"{service} returned invalid inspection JSON.") from None
    if not isinstance(value, dict):
        raise InspectionError(f"{service} returned an unsupported inspection response.")
    return value
