"""Durable projects. References never copy content or confer permissions.

The caller supplies an internal, authorization-aware source resolver, never browser
metadata. A missing resolver/source fails closed. Runtime context assembly remains
separate from this metadata selection operation.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


ProjectId = Annotated[str, StringConstraints(pattern=r"^project_[0-9a-f]{32}$")]
SessionId = Annotated[str, StringConstraints(pattern=r"^ses_[0-9a-f]{16}$")]
TaskId = Annotated[str, StringConstraints(pattern=r"^tsk_[0-9a-f]{32}$")]
FileId = Annotated[str, StringConstraints(pattern=r"^attachment_[0-9a-f]{32}$")]


class Fields(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=2000)
    instructions: str = Field(max_length=16000)

    @field_validator("name", "description")
    @classmethod
    def strip(cls, value, info):
        value = value.strip()
        if info.field_name == "name" and not value:
            raise ValueError("Provide a project name.")
        return value


class Conversation(Strict):
    kind: Literal["conversation"]
    sessionId: SessionId


class Task(Strict):
    kind: Literal["task"]
    taskId: TaskId


class File(Strict):
    kind: Literal["file"]
    sessionId: SessionId
    fileId: FileId


Reference = Annotated[Conversation | Task | File, Field(discriminator="kind")]


class Privacy(Strict):
    memoryDisabled: bool
    harnessDisabled: bool
    incognito: bool = False


class Source(Strict):
    originSessionId: SessionId
    label: str = Field(min_length=1, max_length=500)
    archived: bool
    privacy: Privacy


class LinkSnapshot(Strict):
    originSessionId: SessionId
    linkedAt: float = Field(ge=0, allow_inf_nan=False)


class ProjectLink(Strict):
    reference: Reference
    mode: Literal["live-reference"]
    snapshot: LinkSnapshot
    availability: Literal["available", "archived", "unavailable", "origin-changed"]
    label: str = Field(min_length=1, max_length=500)
    labelSource: Literal["live-source", "unavailable"]
    privacy: Privacy | None


class ProjectView(Strict):
    schemaVersion: Literal[1] = 1
    id: ProjectId
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=2000)
    instructions: str = Field(max_length=16000)
    createdAt: float = Field(ge=0, allow_inf_nan=False)
    updatedAt: float = Field(ge=0, allow_inf_nan=False)
    archivedAt: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    links: list[ProjectLink] = Field(max_length=1000)
    authority: Literal["none"] = "none"
    contentIncluded: Literal[False] = False
    grantsInherited: Literal[False] = False


class ProjectCollection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[ProjectView] = Field(max_length=200)
    nextCursor: ProjectId | None = None


class Revision(Strict):
    expected_revision: int = Field(ge=1)


class Update(Revision):
    fields: Fields


class Archive(Revision):
    archived: bool


class Link(Revision):
    reference: Reference


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, fields TEXT NOT NULL,
 links TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
 updated_at REAL NOT NULL, archived_at REAL
);
"""


class ProjectError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _row(db, identity, revision=None):
    row = db.execute("SELECT * FROM projects WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ProjectError("not_found", "Project not found.", 404)
    if revision is not None and row["revision"] != revision:
        raise ProjectError("revision_conflict", "Project changed. Reload before saving.")
    return row


def _source(resolve, reference):
    value = resolve(reference) if resolve else None
    if value is None:
        return None
    try:
        return Source.model_validate(value)
    except ValueError:
        return None


def _links(row, resolve):
    result = []
    for link in json.loads(row["links"]):
        source = _source(resolve, link["reference"])
        changed = source and source.originSessionId != link["snapshot"]["originSessionId"]
        available = source is not None and not changed
        result.append(
            {
                **link,
                "availability": "origin-changed"
                if changed
                else "unavailable"
                if not source
                else "archived"
                if source.archived
                else "available",
                "label": source.label if available else "Unavailable reference",
                "labelSource": "live-source" if available else "unavailable",
                "privacy": source.privacy.model_dump() if available else None,
            }
        )
    return result


def _view(row, resolve=None):
    return ProjectView(
        id=row["id"],
        revision=row["revision"],
        **json.loads(row["fields"]),
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
        archivedAt=row["archived_at"],
        links=_links(row, resolve),
    ).model_dump()


def get(store, identity, resolve=None):
    with store._connect() as db:
        return _view(_row(db, identity), resolve)


def list_projects(store, resolve=None, limit=None, cursor=None):
    with store._connect() as db:
        where, values = "", []
        if cursor is not None:
            position = _row(db, cursor)
            where = "WHERE created_at>? OR (created_at=? AND id>?)"
            values = [position["created_at"], position["created_at"], cursor]
        suffix = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            values.append(limit)
        return [
            _view(row, resolve)
            for row in db.execute(
                f"SELECT * FROM projects {where} ORDER BY created_at,id{suffix}", values
            )
        ]


def create(store, fields: Fields):
    fields = Fields.model_validate(fields.model_dump())
    identity, now = "project_" + uuid.uuid4().hex, time.time()
    with store._connect() as db:
        db.execute(
            "INSERT INTO projects(id,revision,fields,created_at,updated_at) VALUES (?,1,?,?,?)",
            (identity, fields.model_dump_json(), now, now),
        )
        return _view(_row(db, identity))


def mutate(store, identity, body: Revision, operation, resolve=None):
    # One serialized read/check/write transaction; no source mutation.
    body = type(body).model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity, body.expected_revision)
        fields, links, archived = row["fields"], json.loads(row["links"]), row["archived_at"]
        if operation == "remove":
            if archived is None or links:
                raise ProjectError(
                    "not_empty_archived", "Only empty archived projects can be deleted."
                )
            db.execute("DELETE FROM projects WHERE id=?", (identity,))
            db.commit()
            return None
        if operation == "archive":
            archived = (archived or time.time()) if body.archived else None
        else:
            if archived is not None:
                raise ProjectError("archived", "Restore the project before editing.")
            if operation == "update":
                fields = body.fields.model_dump_json()
            elif operation in ("link", "unlink"):
                reference = body.reference.model_dump()
                existing = next((link for link in links if link["reference"] == reference), None)
                if operation == "unlink":
                    links = [link for link in links if link["reference"] != reference]
                else:
                    source = _source(resolve, reference)
                    if source is None or source.archived:
                        raise ProjectError(
                            "unavailable", "Choose an available source with known privacy."
                        )
                    if (
                        existing
                        and existing["snapshot"]["originSessionId"] != source.originSessionId
                    ):
                        raise ProjectError(
                            "origin_changed", "Unlink and review the changed source origin."
                        )
                    if not existing:
                        if len(links) >= 1000:
                            raise ProjectError(
                                "link_limit", "A project supports up to 1000 references."
                            )
                        links.append(
                            {
                                "reference": reference,
                                "mode": "live-reference",
                                "snapshot": {
                                    "originSessionId": source.originSessionId,
                                    "linkedAt": time.time(),
                                },
                            }
                        )
            else:
                raise ValueError("Unknown project operation.")
        if (
            fields == row["fields"]
            and links == json.loads(row["links"])
            and archived == row["archived_at"]
        ):
            result = _view(row, resolve)
            db.commit()
            return result
        db.execute(
            "UPDATE projects SET fields=?,links=?,archived_at=?,revision=revision+1,"
            "updated_at=? WHERE id=?",
            (fields, json.dumps(links), archived, time.time(), identity),
        )
        result = _view(_row(db, identity), resolve)
        db.commit()
        return result


def search(store, identity, query, resolve=None):
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("Search must be at most 500 characters.")
    project = get(store, identity, resolve)
    return {
        "scope": "linked-metadata-only",
        "projectId": identity,
        "projectRevision": project["revision"],
        "links": [
            link
            for link in project["links"]
            if link["labelSource"] == "live-source"
            and query.strip().casefold() in link["label"].casefold()
        ],
    }


def context(store, identity, target: Privacy, resolve=None):
    target = Privacy.model_validate(target.model_dump())
    project = get(store, identity, resolve)
    result = {
        "projectId": identity,
        "projectRevision": project["revision"],
        "references": [],
        "excluded": [],
        "instruction": {"scope": "project", "sourceId": identity, "text": project["instructions"]}
        if project["archivedAt"] is None and project["instructions"].strip()
        else None,
        "contentIncluded": False,
        "grantsInherited": False,
        "memoryWritesAllowed": False,
        "providerAuthorization": "not-evaluated",
    }
    for link in project["links"]:
        state = link["availability"]
        reason = (
            "project-archived"
            if project["archivedAt"] is not None
            else state
            if state in ("unavailable", "origin-changed")
            else "source-archived"
            if state == "archived"
            else "target-private"
            if any(target.model_dump().values())
            else "origin-private"
            if any(link["privacy"].values())
            else None
        )
        if reason:
            result["excluded"].append({"reference": link["reference"], "reason": reason})
        else:
            result["references"].append(link)
    return result
