"""Durable, inert owner artifacts. Source copies are gated on live provenance/privacy."""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from . import citations


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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


SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, title TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT,
 source_session_id TEXT, source_message_id TEXT, source_text TEXT,
 task_id TEXT, task_session_id TEXT, purged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS artifact_versions (
 artifact_id TEXT NOT NULL REFERENCES artifacts(id), version INTEGER NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(artifact_id,version)
);
CREATE TRIGGER IF NOT EXISTS artifact_versions_no_update BEFORE UPDATE ON artifact_versions
BEGIN SELECT RAISE(ABORT,'artifact versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifact_versions_no_replace BEFORE INSERT ON artifact_versions
WHEN EXISTS(SELECT 1 FROM artifact_versions WHERE artifact_id=NEW.artifact_id
 AND version=NEW.version)
BEGIN SELECT RAISE(ABORT,'artifact versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifact_versions_no_delete BEFORE DELETE ON artifact_versions
WHEN NOT EXISTS(SELECT 1 FROM artifacts WHERE id=OLD.artifact_id AND purged=1)
BEGIN SELECT RAISE(ABORT,'artifact versions are immutable'); END;
CREATE INDEX IF NOT EXISTS artifacts_source ON artifacts(source_session_id);
"""


class ArtifactError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _now():
    return datetime.now(UTC).isoformat()


def _row(db, identity, revision=None):
    row = db.execute("SELECT * FROM artifacts WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ArtifactError("not_found", "Artifact not found.", 404)
    if revision is not None and row["revision"] != revision:
        raise ArtifactError("revision_conflict", "Artifact changed. Reload before saving.")
    return row


def _source(db, session_id, message_id, resolve):
    session = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if db.execute("SELECT 1 FROM forgotten_sessions WHERE session_id=?", (session_id,)).fetchone():
        return "source-redacted", None, None
    message = db.execute(
        "SELECT m.role,m.content,t.status,tm.purpose FROM messages m "
        "LEFT JOIN turn_messages tm ON tm.message_id=m.id LEFT JOIN turns t ON t.id=tm.turn_id "
        "WHERE m.session_id=? AND m.id=?",
        (session_id, message_id),
    ).fetchone()
    if session is None or message is None:
        return "source-unavailable", None, None
    text = json.loads(message["content"])
    if (
        message["role"] != "assistant"
        or message["status"] != "complete"
        or message["purpose"] != "final"
        or not isinstance(text, str)
    ):
        return "source-changed", text, None
    try:
        privacy = Privacy.model_validate(resolve(db, session_id)) if resolve else None
    except Exception:
        privacy = None  # Internal resolver outage or invalid privacy must fail closed.
    if privacy is None:
        return "privacy-unknown", text, None
    return (
        "available" if session["status"] == "open" else "source-archived",
        text,
        privacy.model_dump(),
    )


def _task(db, task_id):
    return db.execute(
        "SELECT t.session_id,t.archived_at,s.status, f.session_id AS forgotten "
        "FROM tasks t JOIN sessions s ON s.id=t.session_id "
        "LEFT JOIN forgotten_sessions f ON f.session_id=s.id WHERE t.id=?",
        (task_id,),
    ).fetchone()


def _view(db, row, resolve):
    versions = [
        json.loads(v[0])
        for v in db.execute(
            "SELECT body FROM artifact_versions WHERE artifact_id=? ORDER BY version", (row["id"],)
        )
    ]
    availability, privacy, private = "available", None, False
    source, has_source = None, bool(row["source_session_id"])
    if row["source_session_id"]:
        raw_source = {
            "sessionId": row["source_session_id"],
            "messageId": row["source_message_id"],
        }
        availability, text, privacy = _source(
            db, raw_source["sessionId"], raw_source["messageId"], resolve
        )
        if row["purged"]:
            availability = "source-redacted"
        elif text != row["source_text"] and availability in (
            "available",
            "source-archived",
            "privacy-unknown",
        ):
            availability = "source-changed"
        if availability in ("available", "source-archived", "privacy-unknown"):
            try:
                current = citations.read(db, raw_source["messageId"])
                original = citations.normalize(versions[0].get("citations", []) if versions else [])
                if current != original:
                    availability = "source-changed"
            except ValueError:
                availability = "source-changed"
        private = bool(privacy and any(privacy.values()))
        try:
            source = SourceReference.model_validate(raw_source).model_dump()
        except ValueError:
            availability = "source-unavailable"
    readable = availability in ("available", "source-archived")
    if not readable:
        privacy, private = None, None
    task, task_availability = None, "none"
    if row["task_id"]:
        task = {"taskId": row["task_id"], "originSessionId": row["task_session_id"]}
        live = _task(db, row["task_id"])
        task_availability = (
            "unavailable"
            if not live or live["forgotten"]
            else "origin-changed"
            if live["session_id"] != row["task_session_id"]
            else "archived"
            if live["archived_at"]
            else "available"
        )
    visible_versions = versions if readable else []
    result = ArtifactView(
        id=row["id"],
        title=row["title"] if readable else "Unavailable artifact",
        revision=row["revision"],
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
        archivedAt=row["archived_at"],
        origin="conversation-copy" if has_source else "owner-authored",
        source=source,
        task=task,
        availability=availability,
        privacy=privacy,
        privateOrigin=private,
        taskAvailability=task_availability,
        contentIncluded=bool(visible_versions),
        versions=visible_versions,
        versionCount=len(versions),
        currentVersion=versions[-1]["version"] if versions else 0,
    ).model_dump(mode="json")
    for version in result["versions"]:
        if version.get("citations") is None:
            version.pop("citations", None)
        if version.get("restoredFromVersion") is None:
            version.pop("restoredFromVersion", None)
    return result


def _summary(view):
    return ArtifactSummary.model_validate(
        {key: value for key, value in view.items() if key not in {"versions", "contentIncluded"}}
    ).model_dump(mode="json")


def get(store, identity, resolve=None):
    with store._connect() as db:
        db.execute("BEGIN")
        return _view(db, _row(db, identity), resolve)


def list_artifacts(store, resolve=None, limit=None, cursor=None):
    with store._connect() as db:
        db.execute("BEGIN")
        where, values = "", []
        if cursor is not None:
            position = _row(db, cursor)
            where = "WHERE created_at>? OR (created_at=? AND id>?)"
            values = [position["created_at"], position["created_at"], cursor]
        suffix = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            values.append(limit)
        return [
            _summary(_view(db, row, resolve))
            for row in db.execute(
                f"SELECT * FROM artifacts {where} ORDER BY created_at,id{suffix}", values
            )
        ]


def create(store, body, resolve=None):
    body = type(body).model_validate(body.model_dump())
    if not isinstance(body, (Create, FromMessage)):
        raise ValueError("Choose an artifact creation request.")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        source_session, source_message, source_text = None, None, None
        copied = isinstance(body, FromMessage)
        if copied:
            status, source_text, _ = _source(db, body.sessionId, body.messageId, resolve)
            if status != "available":
                raise ArtifactError(
                    status,
                    "Choose a completed response with authoritative privacy in an active session.",
                )
            source_session, source_message = body.sessionId, body.messageId
            data = content({"kind": "markdown", "text": source_text})
        else:
            data = content(body.content.model_dump(exclude_none=True))
        task_session = None
        if body.taskId:
            task = _task(db, body.taskId)
            if (
                not task
                or task["archived_at"]
                or task["status"] != "open"
                or task["forgotten"]
                or (source_session and task["session_id"] != source_session)
            ):
                raise ArtifactError(
                    "invalid_task", "Choose an active task in the originating active session."
                )
            task_session = task["session_id"]
        if db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] >= 500:
            raise ArtifactError("limit", "The artifact library holds at most 500 artifacts.")
        identity, now = "artifact_" + uuid.uuid4().hex, _now()
        db.execute(
            "INSERT INTO artifacts VALUES(?,1,?,?,?,NULL,?,?,?,?,?,0)",
            (
                identity,
                body.title,
                now,
                now,
                source_session,
                source_message,
                source_text,
                body.taskId,
                task_session,
            ),
        )
        version = {
            "version": 1,
            "title": body.title,
            "content": data,
            "createdAt": now,
            "author": "source-copy" if copied else "owner",
            "note": "Explicit copy of a completed response." if copied else "Created by owner.",
        }
        if copied:
            evidence = citations.read(db, source_message)
            if evidence:
                version["citations"] = evidence
        db.execute("INSERT INTO artifact_versions VALUES(?,?,?)", (identity, 1, encoded(version)))
        result = _view(db, _row(db, identity), resolve)
        db.commit()
        return result


def mutate(store, identity, body, resolve=None):
    body = type(body).model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity, body.expected_revision)
        view = _view(db, row, resolve)
        now, title, archived = _now(), row["title"], row["archived_at"]
        if isinstance(body, Archive):
            if bool(archived) == body.archived:
                return view
            archived = now if body.archived else None
        else:
            if archived or view["availability"] != "available":
                raise ArtifactError(
                    "not_editable",
                    "Restore an available unchanged source and artifact before editing.",
                )
            versions = view["versions"]
            if isinstance(body, Restore):
                selected = next((v for v in versions if v["version"] == body.version), None)
                if selected is None:
                    raise ArtifactError("invalid_version", "Choose an existing version.", 422)
                data, title, note = (
                    selected["content"],
                    selected["title"],
                    f"Restored version {body.version} as a new version.",
                )
            elif isinstance(body, Append):
                data, title, note = (
                    content(body.content.model_dump(exclude_none=True)),
                    body.title or title,
                    body.note if body.note is not None else "Edited by owner.",
                )
            else:
                raise ValueError("Unknown artifact mutation.")
            version = {
                "version": versions[-1]["version"] + 1,
                "title": title,
                "content": data,
                "note": note,
                "createdAt": now,
                "author": "owner",
            }
            evidence = []
            if isinstance(body, Restore):
                version["restoredFromVersion"] = body.version
                evidence = selected.get("citations", [])
            elif data["kind"] == "markdown" and body.preserveCitations:
                evidence = versions[-1].get("citations", [])
            if evidence:
                version["citations"] = evidence
            if (
                len(versions) >= 100
                or len(encoded([*versions, version]).encode("utf-16-le")) // 2 > 4_000_000
            ):
                raise ArtifactError("limit", "Artifact history reached its retention limit.")
            db.execute(
                "INSERT INTO artifact_versions VALUES(?,?,?)",
                (identity, version["version"], encoded(version)),
            )
        db.execute(
            "UPDATE artifacts SET title=?,archived_at=?,updated_at=?,revision=revision+1 "
            "WHERE id=? AND revision=?",
            (title, archived, now, identity, body.expected_revision),
        )
        result = _view(db, _row(db, identity), resolve)
        db.commit()
        return result


def export(store, identity, version=None, resolve=None, *, format="native"):
    view = get(store, identity, resolve)
    if view["availability"] not in ("available", "source-archived"):
        raise ArtifactError("source_unavailable", "Source privacy or availability blocks export.")
    if version is not None and (type(version) is not int or version < 1):
        raise ArtifactError("invalid_version", "Choose an existing version.", 422)
    selected = (
        view["versions"][-1]
        if version is None
        else next((v for v in view["versions"] if v["version"] == version), None)
    )
    if selected is None:
        raise ArtifactError("invalid_version", "Choose an existing version.", 422)
    data, mime, extension = selected["content"], "text/plain;charset=utf-8", "txt"
    kind = data["kind"]
    if (
        format not in ("native", "docx", "xlsx")
        or (format == "docx" and kind != "markdown")
        or (format == "xlsx" and kind != "table")
    ):
        raise ArtifactError(
            "unsupported_format", "Choose a format supported by this artifact.", 422
        )
    if kind == "table":
        output = io.StringIO(newline="")
        writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        for row in [data["columns"], *data["rows"]]:
            writer.writerow(
                [
                    "'" + cell
                    if cell.startswith(("\t", "\r", "\n"))
                    or re.match(r"^[\s\x00-\x1f]*[=+@-]", cell)
                    else cell
                    for cell in row
                ]
            )
        text, mime, extension = (
            output.getvalue().removesuffix("\r\n"),
            "text/csv;charset=utf-8",
            "csv",
        )
    elif kind in ("chart", "diagram", "media"):
        text, mime, extension = (
            json.dumps(data, ensure_ascii=False, indent=2),
            "application/json;charset=utf-8",
            "json",
        )
    else:
        text = data["text"]
        if kind == "markdown" and selected.get("citations"):
            appendix = json.dumps(selected["citations"], ensure_ascii=False, indent=2).replace(
                "`", "\\u0060"
            )
            text += (
                "\n\n## Supplied source references\n\n"
                "These references were supplied with the source response; "
                "they were not independently verified.\n\n```json\n" + appendix + "\n```\n"
            )
        extensions = {
            "javascript": "js",
            "js": "js",
            "typescript": "ts",
            "ts": "ts",
            "jsx": "jsx",
            "tsx": "tsx",
            "python": "py",
            "py": "py",
            "json": "json",
            "css": "css",
            "sql": "sql",
            "bash": "sh",
            "yaml": "yaml",
            "markdown": "md",
            "html": "html.txt",
            "svg": "svg.txt",
            "xml": "xml.txt",
        }
        extension = (
            "html.txt"
            if kind == "html"
            else "md"
            if kind == "markdown"
            else extensions.get(data["language"].lower(), "txt")
        )
    base = (
        re.sub(r"[^a-zA-Z0-9_-]+", "-", unicodedata.normalize("NFKD", selected["title"])).strip(
            "-"
        )[:80]
        or "artifact"
    )
    if re.fullmatch(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", base, re.I):
        base = "artifact-" + base
    if format != "native":
        from .office_exports import render

        content = render(data, text, format)
        return {
            "filename": f"{base}-v{selected['version']}.{format}",
            "mime": "application/vnd.openxmlformats-officedocument."
            + ("spreadsheetml.sheet" if format == "xlsx" else "wordprocessingml.document"),
            "content": content,
        }
    return NativeExport(
        artifactId=identity,
        version=selected["version"],
        filename=f"{base}-v{selected['version']}.{extension}",
        mime=mime,
        text=text,
        privateOrigin=view["privateOrigin"] is True,
    ).model_dump()


def redact(db, session_ids):
    """Offline forgetting hook; caller owns transaction and vacuum/checkpoint policy."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
    ).fetchone():
        return
    for session_id in session_ids:
        db.execute(
            "UPDATE artifacts SET purged=1,title='Unavailable artifact',source_text=NULL,"
            "revision=revision+1,updated_at=? WHERE source_session_id=? AND purged=0",
            (_now(), session_id),
        )
        db.execute(
            "DELETE FROM artifact_versions WHERE artifact_id IN "
            "(SELECT id FROM artifacts WHERE source_session_id=? AND purged=1)",
            (session_id,),
        )
