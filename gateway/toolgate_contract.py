"""Fixed ToolGate owner routes available to gateway host operations."""

from __future__ import annotations

import re

_REQUEST_ID = r"[A-Za-z0-9_-]{1,128}"
_REQUEST_DETAIL = re.compile(rf"^/v2/owner/requests/{_REQUEST_ID}$")
_REQUEST_DECISION = re.compile(rf"^/v2/owner/requests/{_REQUEST_ID}/decision$")
_DRAFT_ID = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
_DRAFT = re.compile(rf"^/v2/owner/editor-drafts/{_DRAFT_ID}$")
_DRAFT_OWNER_READ = re.compile(
    rf"^/v2/owner/editor-drafts/{_DRAFT_ID}/(?:publications|validation)$"
)
_DRAFT_PUBLISH = re.compile(rf"^/v2/owner/editor-drafts/{_DRAFT_ID}/publish$")
_DRAFT_EXECUTION = re.compile(rf"^/v2/owner/editor-drafts/{_DRAFT_ID}/(?:access|runs)$")


def owner_request_allowed(method: str, path: str) -> bool:
    """Return whether a host operation is one fixed approval-inbox route."""

    if method == "GET":
        return path == "/v2/owner/requests" or _REQUEST_DETAIL.fullmatch(path) is not None
    return method == "POST" and _REQUEST_DECISION.fullmatch(path) is not None


def owner_editor_allowed(method: str, path: str, *, execution: bool = False) -> bool:
    """Return whether one fixed editor route belongs to the requested channel."""

    if execution:
        return method in {"GET", "POST"} and _DRAFT_EXECUTION.fullmatch(path) is not None
    if method == "GET":
        return (
            path in {"/v2/owner/editor-capabilities", "/v2/owner/editor-drafts"}
            or _DRAFT.fullmatch(path) is not None
            or _DRAFT_OWNER_READ.fullmatch(path) is not None
        )
    return method == "POST" and (
        _DRAFT.fullmatch(path) is not None or _DRAFT_PUBLISH.fullmatch(path) is not None
    )
