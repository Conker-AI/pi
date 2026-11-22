"""Request and response models for artifacts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from pi.conversation import citations

# The request side lives in pi.core, where the owner gateway reads it too; artifacts.py and the
# routes still reach it through here.
from pi.core.artifact_requests import (  # noqa: F401
    Append,
    Archive,
    Content,
    Create,
    FromMessage,
    MessageId,
    Restore,
    SessionId,
    TaskId,
    Title,
    content,
    encoded,
)
from pi.core.models import Strict

ArtifactId = Annotated[str, StringConstraints(pattern=r"^artifact_[0-9a-f]{32}$")]


CitationRecord = citations.Citation


Timestamp = Annotated[
    str,
    StringConstraints(
        min_length=20,
        max_length=40,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
    ),
]


class Privacy(Strict):
    memoryDisabled: bool
    harnessDisabled: bool
    incognito: bool = False


class SourceReference(Strict):
    sessionId: SessionId
    messageId: MessageId


class TaskReference(Strict):
    taskId: TaskId
    originSessionId: SessionId


class ArtifactVersion(Strict):
    version: int = Field(ge=1, le=100)
    title: Title
    content: Content
    createdAt: Timestamp
    author: Literal["source-copy", "owner"]
    note: str = Field(max_length=1000)
    restoredFromVersion: int | None = Field(default=None, ge=1, le=100)
    citations: list[CitationRecord] | None = Field(default=None, max_length=100)


Availability = Literal[
    "available",
    "source-archived",
    "source-redacted",
    "source-unavailable",
    "source-changed",
    "privacy-unknown",
]


TaskAvailability = Literal["none", "available", "archived", "unavailable", "origin-changed"]


class ArtifactSummary(Strict):
    schemaVersion: Literal[1] = 1
    id: ArtifactId
    title: Title
    revision: int = Field(ge=1)
    createdAt: Timestamp
    updatedAt: Timestamp
    archivedAt: Timestamp | None
    provenance: Literal["pi"] = "pi"
    origin: Literal["conversation-copy", "owner-authored"]
    source: SourceReference | None
    task: TaskReference | None
    availability: Availability
    privacy: Privacy | None
    privateOrigin: bool | None
    taskAvailability: TaskAvailability
    authority: Literal["none"] = "none"
    contentIncluded: Literal[False] = False
    execution: Literal["not-wired"] = "not-wired"
    versionCount: int = Field(ge=0, le=100)
    currentVersion: int = Field(ge=0, le=100)


class ArtifactView(ArtifactSummary):
    # The summary pins it to False; a view may say True (pyright wants a default for the override).
    contentIncluded: bool  # pyright: ignore[reportGeneralTypeIssues]
    versions: list[ArtifactVersion] = Field(max_length=100)


class ArtifactCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[ArtifactSummary] = Field(max_length=100)
    nextCursor: ArtifactId | None = None


class NativeExport(Strict):
    schemaVersion: Literal[1] = 1
    artifactId: ArtifactId
    version: int = Field(ge=1, le=100)
    filename: str = Field(min_length=1, max_length=110)
    mime: Literal[
        "text/plain;charset=utf-8",
        "text/csv;charset=utf-8",
        "application/json;charset=utf-8",
    ]
    text: str = Field(max_length=400_000)
    provenance: Literal["pi"] = "pi"
    privateOrigin: bool
    authority: Literal["none"] = "none"
    contentIncluded: Literal[True] = True
    execution: Literal["not-wired"] = "not-wired"
