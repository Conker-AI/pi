"""Durable authored agent profiles, not execution identities or permission grants."""

from __future__ import annotations

import json
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def references(values):
    if any(not value.strip() or value != value.strip() or len(value) > 200 for value in values):
        raise ValueError("Use nonempty reference IDs of at most 200 characters.")
    if len(set(values)) != len(values):
        raise ValueError("Reference IDs must be unique.")
    return values


class MemorySelection(StrictModel):
    scope: Literal["none", "conversation", "selected"]
    memoryIds: list[str] = Field(max_length=1000)

    @field_validator("memoryIds")
    @classmethod
    def ids(cls, values):
        return references(values)

    @model_validator(mode="after")
    def selection(self):
        if (self.scope == "selected") != bool(self.memoryIds):
            raise ValueError("Only selected memory scope requires record IDs.")
        return self


class AgentInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=8000)
    modelId: str | None = Field(max_length=200)
    toolIds: list[str] = Field(max_length=1000)
    memory: MemorySelection

    @field_validator("name", "role", "instructions")
    @classmethod
    def text(cls, value):
        if not value.strip():
            raise ValueError("Provide nonempty text.")
        return value.strip()

    @field_validator("modelId")
    @classmethod
    def model(cls, value):
        if value is not None:
            references([value])
        return value

    @field_validator("toolIds")
    @classmethod
    def tools(cls, values):
        return references(values)


class UpdateAgent(StrictModel):
    expected_revision: int = Field(ge=1)
    configuration: AgentInput


class ArchiveAgent(StrictModel):
    expected_revision: int = Field(ge=1)
    archived: bool


class AgentError(Exception):
    def __init__(self, code, message, status=409, current_revision=None):
        super().__init__(message)
        self.status = status
        self.detail = {"code": code, "message": message}
        if current_revision is not None:
            self.detail["current_revision"] = current_revision


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('companion','agent')),
    name_key TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL CHECK(revision>=1),
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS singular_companion ON agents(kind) WHERE kind='companion';
CREATE TABLE IF NOT EXISTS agent_versions (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    configuration TEXT NOT NULL,
    archived_at REAL,
    recorded_at REAL NOT NULL,
    change_kind TEXT NOT NULL CHECK(change_kind IN ('created','updated','archived','restored')),
    PRIMARY KEY(agent_id,revision)
);
CREATE TRIGGER IF NOT EXISTS agent_versions_no_update BEFORE UPDATE ON agent_versions
BEGIN SELECT RAISE(ABORT,'agent versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agent_versions_no_delete BEFORE DELETE ON agent_versions
BEGIN SELECT RAISE(ABORT,'agent versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agent_versions_no_replace BEFORE INSERT ON agent_versions
WHEN EXISTS(SELECT 1 FROM agent_versions WHERE agent_id=NEW.agent_id AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'agent versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agents_no_delete BEFORE DELETE ON agents
BEGIN SELECT RAISE(ABORT,'archive agents to preserve history'); END;
CREATE TRIGGER IF NOT EXISTS agents_no_replace BEFORE INSERT ON agents
WHEN EXISTS(SELECT 1 FROM agents WHERE id=NEW.id OR name_key=NEW.name_key)
BEGIN SELECT RAISE(ABORT,'agent identity already exists'); END;
CREATE TRIGGER IF NOT EXISTS agents_identity_immutable BEFORE UPDATE ON agents
WHEN NEW.id!=OLD.id OR NEW.kind!=OLD.kind OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'agent identity is immutable'); END;
"""


def initialize(db):
    db.executescript(SCHEMA)
    db.execute("BEGIN IMMEDIATE")
    if not db.execute("SELECT 1 FROM agents WHERE kind='companion'").fetchone():
        configuration = AgentInput(
            name="Conker",
            role="The daily companion",
            instructions="Act as the owner's daily companion.",
            modelId=None,
            toolIds=[],
            memory=MemorySelection(scope="conversation", memoryIds=[]),
        )
        _insert(db, "companion", "companion", configuration, time.time())
    db.commit()


def _insert(db, identity, kind, configuration, now):
    db.execute(
        "INSERT INTO agents VALUES (?,?,?,1,?)",
        (identity, kind, configuration.name.casefold(), now),
    )
    db.execute(
        "INSERT INTO agent_versions VALUES (?,1,?,NULL,?,'created')",
        (identity, configuration.model_dump_json(), now),
    )


def _get(db, identity, revision=None):
    agent = db.execute("SELECT * FROM agents WHERE id=?", (identity,)).fetchone()
    if agent is None:
        raise AgentError("not_found", "Agent does not exist.", 404)
    version = db.execute(
        "SELECT * FROM agent_versions WHERE agent_id=? AND revision=?",
        (identity, agent["revision"] if revision is None else revision),
    ).fetchone()
    if version is None:
        raise AgentError("version_not_found", "Agent version does not exist.", 404)
    return {
        "id": identity,
        "kind": agent["kind"],
        "revision": version["revision"],
        "configuration": json.loads(version["configuration"]),
        "created_at": agent["created_at"],
        "updated_at": version["recorded_at"],
        "archived_at": version["archived_at"],
        "change_kind": version["change_kind"],
        "authority": "none",
        "execution": "not-integrated",
        "reference_validation": "not-performed",
    }


def get(store, identity, revision=None):
    with store._connect() as db:
        # One read transaction keeps current revision and version coherent.
        db.execute("BEGIN")
        return _get(db, identity, revision)


def list_agents(store):
    with store._connect() as db:
        db.execute("BEGIN")
        return {
            "results": [
                _get(db, row["id"])
                for row in db.execute("SELECT id FROM agents ORDER BY created_at,id").fetchall()
            ]
        }


def history(store, identity):
    with store._connect() as db:
        db.execute("BEGIN")
        _get(db, identity)
        return {
            "results": [
                _get(db, identity, row["revision"])
                for row in db.execute(
                    "SELECT revision FROM agent_versions WHERE agent_id=? ORDER BY revision",
                    (identity,),
                ).fetchall()
            ]
        }


def _unique_name(db, configuration, identity=None):
    other = db.execute(
        "SELECT id FROM agents WHERE name_key=?", (configuration.name.casefold(),)
    ).fetchone()
    if other is not None and other["id"] != identity:
        raise AgentError(
            "name_conflict", "Choose a unique name, including archived agents and the Companion."
        )


def create(store, configuration: AgentInput):
    configuration = AgentInput.model_validate(configuration.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _unique_name(db, configuration)
        identity = "agent_" + uuid.uuid4().hex
        _insert(db, identity, "agent", configuration, time.time())
        result = _get(db, identity)
        db.commit()
    return result


def _editable(db, identity, expected_revision):
    current = _get(db, identity)
    if current["kind"] == "companion":
        raise AgentError("companion_protected", "The singular Companion is managed separately.")
    if current["revision"] != expected_revision:
        raise AgentError(
            "revision_conflict",
            "Agent changed. Reload before saving.",
            current_revision=current["revision"],
        )
    return current


def _append(db, current, configuration, archived_at, change):
    revision = current["revision"] + 1
    db.execute(
        "INSERT INTO agent_versions VALUES (?,?,?,?,?,?)",
        (
            current["id"],
            revision,
            configuration.model_dump_json(),
            archived_at,
            time.time(),
            change,
        ),
    )
    db.execute(
        "UPDATE agents SET revision=?,name_key=? WHERE id=?",
        (revision, configuration.name.casefold(), current["id"]),
    )
    return _get(db, current["id"])


def update(store, identity, request: UpdateAgent):
    request = UpdateAgent.model_validate(request.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _editable(db, identity, request.expected_revision)
        if current["archived_at"] is not None:
            raise AgentError("archived", "Restore this agent before editing it.")
        _unique_name(db, request.configuration, identity)
        result = _append(db, current, request.configuration, None, "updated")
        db.commit()
    return result


def archive(store, identity, request: ArchiveAgent):
    request = ArchiveAgent.model_validate(request.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _editable(db, identity, request.expected_revision)
        if (current["archived_at"] is not None) == request.archived:
            return current
        result = _append(
            db,
            current,
            AgentInput.model_validate(current["configuration"]),
            time.time() if request.archived else None,
            "archived" if request.archived else "restored",
        )
        db.commit()
    return result
