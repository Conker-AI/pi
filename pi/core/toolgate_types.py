"""What a ToolGate call can return or raise: tools, approvals, results, refusals.

Pure data and exception types, kept in core so a package that only reads ToolGate's answers
(host, setup) need not import the client. `pi.actions.toolgate` re-exports every name, so the
client's callers keep importing them from there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    description: str
    inputs: list[dict]
    # ToolGate's default for this tool, and whether it is on the owner's floor (always asks).
    authorization: str = "owner_confirmation"
    sensitive: bool = False
    # The Inbox card template the owner picked for it, when ToolGate lists one ("do" otherwise).
    card: str = "do"
    # A tool on an external MCP server: its name and description came from that server.
    external: bool = False


@dataclass(frozen=True)
class ApprovalRequired:
    """ToolGate wants the owner to confirm this exact action.

    Not an error: the turn parks rather than failing. The owner has not said no,
    they have not been asked yet.
    """

    request_id: str
    expires_at: str | None
    message: str
    tool_id: str
    args: dict


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    result: Any
    tool_id: str
    # Whether the world changed. None: it did if `ok` (every ToolGate tool). Only a tool that
    # changes nothing (a read) or knows exactly what it changed says so.
    acted: bool | None = None

    @property
    def changed(self) -> bool:
        """Whether this result marks its turn acted."""
        return self.ok if self.acted is None else self.acted


@dataclass(frozen=True)
class ToolPending:
    status: str
    message: str
    action_id: str


class ToolGateUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ToolRefused(RuntimeError):
    """ToolGate declined: policy, lockdown, a spent approval, bad arguments.

    A refusal is an answer. It is surfaced to the model and recorded, never
    retried with the guard removed.
    """

    def __init__(self, code: str, message: str, next_action: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.next_action = next_action
