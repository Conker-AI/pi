"""Request bodies of the chat context routes: policy, reviewed fork, summaries, compaction and decisions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from pi.core.context_policy import Policy
from pi.core.models import Strict


class Update(Strict):
    expected_revision: int = Field(ge=0)
    policy: Policy


class ReviewedFork(Strict):
    expected_revision: int = Field(ge=1)
    expected_last_message_id: str | None = Field(default=None, max_length=200)
    summary: str = Field(max_length=16000)
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class Edit(Strict):
    expected_revision: int = Field(ge=0)
    summary: str = Field(max_length=16000)


class Restore(Strict):
    expected_revision: int = Field(ge=0)
    revision: int = Field(ge=0)


class Request(Strict):
    focus: str = Field(default="", max_length=500)


class Compact(Strict):
    expected_policy_revision: int = Field(ge=1)
    expected_summary_revision: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=16000)
    # Distinct message ids of this chat (or inherited by it).
    covers: list[str] = Field(min_length=1, max_length=10000)


class Add(Strict):
    expected_revision: int = Field(ge=0)
    kind: Literal["decision", "thread"]
    text: str = Field(min_length=1, max_length=500)
    author: Literal["owner", "ai"] = "owner"
    cites: list[str] = Field(default_factory=list, max_length=10)
    replaces: str | None = Field(default=None, min_length=1, max_length=64)


class State(Strict):
    expected_revision: int = Field(ge=0)
    state: Literal["active", "open", "resolved", "withdrawn"]
    by: Literal["owner", "ai"] = "owner"
