"""Request and response models for artifacts."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from pi.conversation import citations
from pi.core.models import Strict

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


NodeID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]


ArtifactId = Annotated[str, StringConstraints(pattern=r"^artifact_[0-9a-f]{32}$")]


SessionId = Annotated[str, StringConstraints(pattern=r"^ses_[0-9a-f]{16}$")]


MessageId = Annotated[str, StringConstraints(pattern=r"^msg_[0-9a-f]{16}$")]


TaskId = Annotated[str, StringConstraints(pattern=r"^tsk_[0-9a-f]{32}$")]


CitationRecord = citations.Citation


Timestamp = Annotated[
    str,
    StringConstraints(
        min_length=20,
        max_length=40,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
    ),
]


class Text(Strict):
    kind: Literal["html", "markdown"]
    text: str = Field(max_length=200_000)


class Code(Strict):
    kind: Literal["code"]
    text: str = Field(max_length=200_000)
    language: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9+#._-]{0,40}$")
    ]


class Media(Strict):
    kind: Literal["media"]
    mediaType: Literal["image", "audio", "video"]
    url: str = Field(max_length=2048)
    description: str = Field(max_length=2000)

    @model_validator(mode="after")
    def normalize(self):
        self.url = self.url.strip()
        if self.url:
            parts = urlsplit(self.url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or any(ord(c) < 33 for c in self.url)
                or "\\" in self.url
            ):
                raise ValueError("Use a complete HTTPS URL without credentials.")
            _ = parts.port
            self.url = urlunsplit(
                ("https", parts.netloc.lower(), parts.path or "/", parts.query, parts.fragment)
            )
        return self


class Table(Strict):
    kind: Literal["table"]
    columns: list[Annotated[str, Field(max_length=1000)]] = Field(min_length=1, max_length=32)
    rows: list[Annotated[list[Annotated[str, Field(max_length=10000)]], Field(max_length=32)]] = (
        Field(max_length=1000)
    )

    @model_validator(mode="after")
    def widths(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Every table row must match the columns.")
        return self


class Node(Strict):
    id: NodeID
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: str | None = Field(default=None, max_length=2000)
    x: float = Field(ge=-10000, le=10000, allow_inf_nan=False)
    y: float = Field(ge=-10000, le=10000, allow_inf_nan=False)


class Edge(Strict):
    id: NodeID
    source: NodeID
    target: NodeID
    label: str | None = Field(default=None, max_length=200)


class Diagram(Strict):
    kind: Literal["diagram"]
    nodes: list[Node] = Field(max_length=100)
    edges: list[Edge] = Field(max_length=200)

    @model_validator(mode="after")
    def references(self):
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes) or len({e.id for e in self.edges}) != len(self.edges):
            raise ValueError("Diagram identities must be distinct.")
        if any(
            e.source not in ids or e.target not in ids or e.source == e.target for e in self.edges
        ):
            raise ValueError("Edges must join two existing, distinct nodes.")
        return self


class Series(Strict):
    label: str = Field(min_length=1, max_length=200)


class ChartRow(Strict):
    label: str = Field(max_length=1000)
    values: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(min_length=1, max_length=8)


class Chart(Strict):
    kind: Literal["chart"]
    chartType: Literal["bar", "line", "area"]
    xLabel: str = Field(max_length=1000)
    series: list[Series] = Field(min_length=1, max_length=8)
    rows: list[ChartRow] = Field(max_length=500)

    @model_validator(mode="after")
    def widths(self):
        if len({s.label for s in self.series}) != len(self.series):
            raise ValueError("Series labels must be distinct.")
        if any(len(row.values) != len(self.series) for row in self.rows):
            raise ValueError("Every chart row must match the series count.")
        return self


Content = Annotated[Text | Code | Media | Table | Diagram | Chart, Field(discriminator="kind")]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def content(value):
    result = TypeAdapter(Content).validate_python(value).model_dump(exclude_none=True)
    if len(encoded(result).encode("utf-16-le")) // 2 > 250_000:
        raise ValueError("Content exceeds 250,000 serialized characters.")
    return result


class Create(Strict):
    title: Title
    content: Content
    taskId: TaskId | None = None


class FromMessage(Strict):
    title: Title
    sessionId: SessionId
    messageId: MessageId
    taskId: TaskId | None = None


class Revision(Strict):
    expected_revision: int = Field(ge=1, le=9007199254740991)


class Append(Revision):
    content: Content
    title: Title | None = None
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    preserveCitations: bool = True


class Restore(Revision):
    version: int = Field(ge=1, le=9007199254740991)


class Archive(Revision):
    archived: bool


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
    contentIncluded: bool
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
