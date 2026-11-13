"""The replies of the Inbox routes for Conker's questions and change requests, described in
OpenAPI (D-01). They name what `pi.agents.asks` and `pi.agents.change_requests` already send;
the routes describe them with `pi.core.models.described` and do not re-validate the reply."""

from typing import Literal

from pi.agents.change_requests import View
from pi.core.models import Strict


class AskView(Strict):
    """One question; `chatTitle` is "Private chat" for a private one."""

    id: str
    sessionId: str
    chatTitle: str
    question: str
    options: list[str]
    state: Literal["open", "answered", "dismissed"]
    answer: str | None
    chosen: bool | None
    createdAt: float
    answeredAt: float | None


class AskList(Strict):
    schemaVersion: Literal[1]
    results: list[AskView]


class ChangeRequestList(Strict):
    schemaVersion: Literal[1]
    results: list[View]
