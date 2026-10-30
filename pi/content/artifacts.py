"""Durable, inert owner artifacts. Source copies are gated on live provenance/privacy."""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import UTC, datetime

from pi.content import office_exports
from pi.content.artifact_models import (
    Append,
    Archive,
    ArtifactSummary,
    ArtifactView,
    Create,
    FromMessage,
    NativeExport,
    Privacy,
    Restore,
    SourceReference,
    content,
    encoded,
)
from pi.conversation import citations
from pi.core.errors import ServiceError

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


class ArtifactError(ServiceError):
    pass


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
    plain, readable = office_exports.names(selected["title"])
    if format != "native":
        if not office_exports.supports(kind, format):
            raise ArtifactError(
                "unsupported_format", "Choose a format supported by this artifact.", 422
            )
        suffix = f"-v{selected['version']}.{format}"
        return {
            "filename": plain + suffix,
            "readable": readable + suffix,
            "mime": office_exports.MIME[format],
            "content": office_exports.render(
                data,
                format,
                title=selected["title"],
                citations=selected.get("citations") if kind == "markdown" else None,
            ),
        }
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
    return NativeExport(
        artifactId=identity,
        version=selected["version"],
        filename=f"{plain}-v{selected['version']}.{extension}",
        mime=mime,
        text=text,
        privateOrigin=view["privateOrigin"] is True,
    ).model_dump()


def _answer_title(text):
    for line in text.splitlines():
        words = re.sub(r"[#*_`>|~\[\]()!-]+", " ", line).split()
        if words:
            return " ".join(words)[:160]
    return "Conker answer"


def export_answer(store, session_id, message_id, resolve=None, *, format):
    """One completed answer as a file, under the same source and privacy checks as a copy."""
    if not office_exports.supports("markdown", format):
        raise ArtifactError("unsupported_format", "Choose DOCX, PDF, PPTX or XLSX.", 422)
    with store._connect() as db:
        db.execute("BEGIN")
        status, text, _ = _source(db, session_id, message_id, resolve)
        if status in ("source-unavailable", "source-redacted"):
            raise ArtifactError("not_found", "Answer not found.", 404)
        if status == "source-changed":
            raise ArtifactError(
                "not_an_answer", "Only Conker's completed final answers can be exported.", 422
            )
        if status == "privacy-unknown":
            raise ArtifactError("privacy_unknown", "This chat's privacy is unknown; try again.")
        try:
            evidence = citations.read(db, message_id)
        except ValueError:
            raise ArtifactError(
                "source_changed", "This answer's sources are unreadable; it was not exported."
            ) from None
    title = _answer_title(text)
    plain, readable = office_exports.names(title)
    return {
        "filename": f"{plain}.{format}",
        "readable": f"{readable}.{format}",
        "mime": office_exports.MIME[format],
        "content": office_exports.render(
            {"kind": "markdown", "text": text}, format, title=title, citations=evidence
        ),
    }


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
