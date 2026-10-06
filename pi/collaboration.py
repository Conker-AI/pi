"""Stored templates and bounded team preparations; never a dispatch or grant."""

import json
import time
import uuid
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from . import agents


class Budget(agents.StrictModel):
    maxTurns: int = Field(ge=1, le=200)
    maxTokens: int = Field(ge=1, le=1000000)
    maxCostCents: int = Field(ge=0, le=1000000)


class TeamBudget(Budget):
    maxHandoffs: int = Field(ge=0, le=100)


class Context(agents.StrictModel):
    mode: Literal["task_only", "selected"]
    sourceIds: list[str] = Field(max_length=100)

    @field_validator("sourceIds")
    @classmethod
    def ids(cls, values):
        return agents.references(values)

    @model_validator(mode="after")
    def selected(self):
        if (self.mode == "selected") != bool(self.sourceIds):
            raise ValueError("Only selected context requires explicit source IDs.")
        return self


class Role(agents.StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    agentId: str = Field(pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    instructions: str = Field(min_length=1, max_length=8000)
    toolIds: list[str] = Field(max_length=100)
    memory: agents.MemorySelection
    context: Context
    budget: Budget

    @field_validator("name", "instructions")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)

    @field_validator("agentId")
    @classmethod
    def identity(cls, value):
        agents.references([value])
        return value

    @field_validator("toolIds")
    @classmethod
    def tools(cls, values):
        return agents.references(values)

    @field_validator("memory")
    @classmethod
    def memory_limit(cls, value):
        if value.scope == "owner":
            raise ValueError("Across-chat owner memory is not available to team roles.")
        if len(value.memoryIds) > 100:
            raise ValueError("Use at most 100 selected memory records.")
        return value


class Handoff(agents.StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    fromRoleId: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    toRoleId: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    condition: str = Field(min_length=1, max_length=1000)
    payload: Literal["result_only", "result_and_citations"]
    maxTransfers: int = Field(ge=1, le=100)

    @field_validator("condition")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)


class Team(agents.StrictModel):
    name: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2000)
    roles: list[Role] = Field(min_length=1, max_length=12)
    handoffs: list[Handoff] = Field(max_length=30)
    budget: TeamBudget

    @field_validator("name", "objective")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)

    @model_validator(mode="after")
    def graph(self):
        ids = [role.id for role in self.roles]
        for values in (
            ids,
            [role.name.casefold() for role in self.roles],
            [edge.id for edge in self.handoffs],
            [(edge.fromRoleId, edge.toRoleId) for edge in self.handoffs],
        ):
            if len(values) != len(set(values)):
                raise ValueError("Role IDs/names, handoff IDs and directed pairs must be unique.")
        for edge in self.handoffs:
            if (
                edge.fromRoleId == edge.toRoleId
                or edge.fromRoleId not in ids
                or edge.toRoleId not in ids
            ):
                raise ValueError("Handoffs connect two different existing roles.")
        if sum(edge.maxTransfers for edge in self.handoffs) > self.budget.maxHandoffs:
            raise ValueError("Handoff allocations exceed the team budget.")
        for field in Budget.model_fields:
            if sum(getattr(role.budget, field) for role in self.roles) > getattr(
                self.budget, field
            ):
                raise ValueError("Role allocations exceed the team " + field + " budget.")
        return self


class Template(agents.StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    agent: agents.AgentInput

    @field_validator("name", "description")
    @classmethod
    def text(cls, value):
        return agents.AgentInput.text(value)

    @field_validator("agent")
    @classmethod
    def limits(cls, value):
        if len(value.toolIds) > 100 or len(value.memory.memoryIds) > 100:
            raise ValueError("Use at most 100 tool or memory selections.")
        return value


class Revision(agents.StrictModel):
    expected_revision: int = Field(ge=1)


class UpdateTemplate(Revision):
    definition: Template


class UpdateTeam(Revision):
    definition: Team


class TeamSummary(agents.StrictModel):
    schemaVersion: Literal[1] = 1
    id: str = Field(pattern=r"^team_[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    archived_at: float | None = Field(ge=0, allow_inf_nan=False)
    created_at: float = Field(ge=0, allow_inf_nan=False)
    updated_at: float = Field(ge=0, allow_inf_nan=False)
    authority: Literal["none"] = "none"
    execution: Literal["configuration-only"] = "configuration-only"
    contentIncluded: Literal[False] = False
    reference_validation: Literal["external-references-unverified"] = (
        "external-references-unverified"
    )


class AgentReference(agents.StrictModel):
    roleId: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    agentId: str = Field(pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    revision: int = Field(ge=1)


class TeamView(TeamSummary):
    definition: Team
    contentIncluded: Literal[True] = True
    agentReferences: list[AgentReference] = Field(max_length=12)
    agentReferenceState: Literal["available", "unavailable", "selection-conflict"]


class TeamCollection(agents.StrictModel):
    schemaVersion: Literal[1] = 1
    results: list[TeamSummary] = Field(max_length=100)
    nextCursor: str | None = Field(default=None, pattern=r"^team_[0-9a-f]{32}$")


class TeamRevision(TeamSummary):
    definition: Team
    contentIncluded: Literal[True] = True
    historical: Literal[True] = True


class TeamHistory(agents.StrictModel):
    schemaVersion: Literal[1] = 1
    results: list[TeamSummary] = Field(max_length=100)
    nextRevision: int | None = Field(default=None, ge=1)


class Instantiate(Revision):
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    # Whole fields only; fully merged configuration is strictly validated below.
    overrides: dict = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def allowed_fields(cls, value):
        if set(value) - (set(agents.AgentInput.model_fields) - {"name"}):
            raise ValueError("Only agent fields other than name may be overridden.")
        return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS collaboration_records (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('template','team')),
    name_key TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
    definition TEXT NOT NULL, archived_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    deleted_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS collaboration_active_names
ON collaboration_records(kind,name_key) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS template_publications (
    template_id TEXT NOT NULL REFERENCES collaboration_records(id),
    version INTEGER NOT NULL CHECK(version>=1), definition TEXT NOT NULL, published_at REAL NOT NULL,
    PRIMARY KEY(template_id,version)
);
CREATE TABLE IF NOT EXISTS collaboration_preparations (
    id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES collaboration_records(id),
    kind TEXT NOT NULL CHECK(kind IN ('template','team')), snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_definition_revisions (
 team_id TEXT NOT NULL REFERENCES collaboration_records(id), revision INTEGER NOT NULL,
 definition TEXT NOT NULL, archived_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(team_id,revision)
);
CREATE TRIGGER IF NOT EXISTS team_definition_insert AFTER INSERT ON collaboration_records
WHEN NEW.kind='team' AND NEW.deleted_at IS NULL
BEGIN INSERT INTO team_definition_revisions VALUES
 (NEW.id,NEW.revision,NEW.definition,NEW.archived_at,NEW.created_at,NEW.updated_at); END;
CREATE TRIGGER IF NOT EXISTS team_definition_update AFTER UPDATE ON collaboration_records
WHEN NEW.kind='team' AND NEW.deleted_at IS NULL
BEGIN INSERT INTO team_definition_revisions VALUES
 (NEW.id,NEW.revision,NEW.definition,NEW.archived_at,NEW.created_at,NEW.updated_at); END;
CREATE TRIGGER IF NOT EXISTS team_definition_identity BEFORE UPDATE ON collaboration_records
WHEN OLD.kind='team' AND (NEW.id!=OLD.id OR NEW.kind!=OLD.kind OR NEW.revision!=OLD.revision+1)
BEGIN SELECT RAISE(ABORT,'team revisions must advance once'); END;
CREATE TRIGGER IF NOT EXISTS team_definition_no_replace BEFORE INSERT ON collaboration_records
WHEN EXISTS(SELECT 1 FROM collaboration_records WHERE id=NEW.id AND kind='team')
BEGIN SELECT RAISE(ABORT,'team identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS collaboration_records_no_delete BEFORE DELETE ON collaboration_records
BEGIN SELECT RAISE(ABORT,'use a retained removal tombstone'); END;
"""
for _operation in ("UPDATE", "DELETE"):
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS team_revisions_no_{_operation.lower()}
BEFORE {_operation} ON team_definition_revisions
BEGIN SELECT RAISE(ABORT,'team revisions are immutable'); END;
"""
SCHEMA += """
CREATE TRIGGER IF NOT EXISTS team_revisions_no_replace BEFORE INSERT ON team_definition_revisions
WHEN EXISTS(SELECT 1 FROM team_definition_revisions
 WHERE team_id=NEW.team_id AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'team revisions are immutable'); END;
"""
for _table in ("template_publications", "collaboration_preparations"):
    for _operation in ("UPDATE", "DELETE"):
        SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_{_operation.lower()} BEFORE {_operation} ON {_table}
BEGIN SELECT RAISE(ABORT,'published definitions and preparations are immutable'); END;
"""
    _match = (
        "template_id=NEW.template_id AND version=NEW.version"
        if _table == "template_publications"
        else "id=NEW.id"
    )
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_replace BEFORE INSERT ON {_table}
WHEN EXISTS(SELECT 1 FROM {_table} WHERE {_match})
BEGIN SELECT RAISE(ABORT,'published definitions and preparations are immutable'); END;
"""


def initialize(db):
    # Legacy definitions retain their actual current revision; missing history is not invented.
    db.execute(
        "INSERT INTO team_definition_revisions "
        "SELECT id,revision,definition,archived_at,created_at,updated_at "
        "FROM collaboration_records r WHERE kind='team' AND deleted_at IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM team_definition_revisions v "
        "WHERE v.team_id=r.id AND v.revision=r.revision)"
    )


def _get(db, identity, kind, revision=None, active=False):
    row = db.execute(
        "SELECT * FROM collaboration_records WHERE id=? AND kind=? AND deleted_at IS NULL",
        (identity, kind),
    ).fetchone()
    if row is None:
        raise agents.AgentError("not_found", "Configuration does not exist.", 404)
    value = dict(row)
    value.pop("name_key")
    value["definition"] = json.loads(value["definition"])
    value.update(
        authority="none",
        execution="not-integrated",
        reference_validation="external-references-unverified",
    )
    if revision is not None and revision != value["revision"]:
        raise agents.AgentError(
            "revision_conflict",
            "Configuration changed. Reload before saving or preparing.",
            current_revision=value["revision"],
        )
    if active and value["archived_at"] is not None:
        raise agents.AgentError("archived", "Restore this configuration first.")
    if kind == "template":
        value["versions"] = [
            {
                "version": row["version"],
                "definition": json.loads(row["definition"]),
                "published_at": row["published_at"],
            }
            for row in db.execute(
                "SELECT * FROM template_publications WHERE template_id=? ORDER BY version",
                (identity,),
            )
        ]
    return value


def get(store, identity, kind):
    with store._connect() as db:
        db.execute("BEGIN")
        return _get(db, identity, kind)


def _team_summary(value):
    definition = Team.model_validate(value["definition"])
    return TeamSummary(
        id=value["id"],
        revision=value["revision"],
        name=definition.name,
        archived_at=value["archived_at"],
        created_at=value["created_at"],
        updated_at=value["updated_at"],
    )


def _team_view(db, value):
    definition = Team.model_validate(value["definition"])
    state, references = "available", []
    try:
        snapshots = _team_agents(db, definition)
        references = [
            AgentReference(
                roleId=item["roleId"], agentId=item["agentId"], revision=item["agentVersion"]
            )
            for item in snapshots
        ]
    except agents.AgentError as exc:
        state = (
            "selection-conflict" if exc.detail["code"] == "selection_widening" else "unavailable"
        )
    return TeamView(
        **{k: v for k, v in _team_summary(value).model_dump().items() if k != "contentIncluded"},
        definition=definition,
        agentReferences=references,
        agentReferenceState=state,
    )


def owner_get(store, identity):
    with store._connect() as db:
        db.execute("BEGIN")
        return _team_view(db, _get(db, identity, "team"))


def owner_list(store, limit=50, cursor=None):
    with store._connect() as db:
        db.execute("BEGIN")
        if cursor is not None:
            _get(db, cursor, "team")
        rows = db.execute(
            "SELECT id FROM collaboration_records WHERE kind='team' AND deleted_at IS NULL "
            "AND (? IS NULL OR id>?) ORDER BY id LIMIT ?",
            (cursor, cursor, limit + 1),
        ).fetchall()
        return TeamCollection(
            results=[_team_summary(_get(db, row["id"], "team")) for row in rows[:limit]],
            nextCursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )


def owner_history(store, identity, limit=50, after=0):
    with store._connect() as db:
        db.execute("BEGIN")
        _get(db, identity, "team")
        rows = db.execute(
            "SELECT * FROM team_definition_revisions WHERE team_id=? AND revision>? "
            "ORDER BY revision LIMIT ?",
            (identity, after, limit + 1),
        ).fetchall()
        return TeamHistory(
            results=[_team_summary(_historical(row)) for row in rows[:limit]],
            nextRevision=rows[limit - 1]["revision"] if len(rows) > limit else None,
        )


def _historical(row):
    return {**dict(row), "id": row["team_id"], "definition": json.loads(row["definition"])}


def owner_revision(store, identity, revision):
    with store._connect() as db:
        db.execute("BEGIN")
        _get(db, identity, "team")
        row = db.execute(
            "SELECT * FROM team_definition_revisions WHERE team_id=? AND revision=?",
            (identity, revision),
        ).fetchone()
        if row is None:
            raise agents.AgentError("version_not_found", "Team revision does not exist.", 404)
        value = _historical(row)
        return TeamRevision(
            **{
                k: v for k, v in _team_summary(value).model_dump().items() if k != "contentIncluded"
            },
            definition=Team.model_validate(value["definition"]),
        )


def list_all(store):
    with store._connect() as db:
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT id,kind FROM collaboration_records WHERE deleted_at IS NULL ORDER BY created_at,id"
        ).fetchall()
        return {
            "templates": [_get(db, r["id"], "template") for r in rows if r["kind"] == "template"],
            "teams": [_get(db, r["id"], "team") for r in rows if r["kind"] == "team"],
            "preparations": [
                json.loads(r["snapshot"])
                for r in db.execute(
                    "SELECT snapshot FROM collaboration_preparations ORDER BY rowid"
                )
            ],
        }


def _team_agents(db, definition):
    snapshots = []
    for role in definition.roles:
        agent = agents._get(db, role.agentId)
        if agent["archived_at"] is not None:
            raise agents.AgentError(
                "agent_archived", "Team roles need active agent configurations."
            )
        base = agent["configuration"]
        if not set(role.toolIds).issubset(base["toolIds"]):
            raise agents.AgentError("selection_widening", "Role tools exceed the agent selection.")
        if role.memory.scope != "none" and (
            role.memory.scope != base["memory"]["scope"]
            or not set(role.memory.memoryIds).issubset(base["memory"]["memoryIds"])
        ):
            raise agents.AgentError(
                "selection_widening", "Role memory exceeds the agent selection."
            )
        snapshots.append(
            {
                "roleId": role.id,
                "agentId": role.agentId,
                "agentVersion": agent["revision"],
                "configuration": base,
            }
        )
    return snapshots


def _validated(kind, definition):
    cls = Template if kind == "template" else Team if kind == "team" else None
    if cls is None:
        raise ValueError("Unsupported configuration kind.")
    return cls.model_validate(definition.model_dump())


def _unique(db, kind, name, identity=None):
    row = db.execute(
        "SELECT id FROM collaboration_records WHERE kind=? AND name_key=? AND deleted_at IS NULL",
        (kind, name.casefold()),
    ).fetchone()
    if row and row["id"] != identity:
        raise agents.AgentError(
            "name_conflict", "Choose a unique configuration name, including archived records."
        )


def save(store, kind, definition, identity=None, revision=None, *, owner_view=False):
    definition = _validated(kind, definition)
    if identity is not None:
        Revision(expected_revision=revision)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if identity is not None:
            _get(db, identity, kind, revision, active=True)
        _unique(db, kind, definition.name, identity)
        if kind == "team":
            _team_agents(db, definition)
        now = time.time()
        if identity is None:
            identity = kind + "_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO collaboration_records VALUES (?,?,?,1,?,NULL,?,?,NULL)",
                (
                    identity,
                    kind,
                    definition.name.casefold(),
                    definition.model_dump_json(),
                    now,
                    now,
                ),
            )
        else:
            db.execute(
                "UPDATE collaboration_records SET name_key=?,definition=?,revision=revision+1,updated_at=? WHERE id=?",
                (definition.name.casefold(), definition.model_dump_json(), now, identity),
            )
        result = _get(db, identity, kind)
        if owner_view:
            result = _team_view(db, result)
        db.commit()
        return result


def archive(store, identity, kind, request: agents.ArchiveAgent, *, owner_view=False):
    request = agents.ArchiveAgent.model_validate(request.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _get(db, identity, kind, request.expected_revision)
        if kind == "team" and not request.archived:
            _team_agents(db, Team.model_validate(current["definition"]))
        if (current["archived_at"] is not None) != request.archived:
            now = time.time()
            db.execute(
                "UPDATE collaboration_records SET archived_at=?,revision=revision+1,updated_at=? WHERE id=?",
                (now if request.archived else None, now, identity),
            )
        result = _get(db, identity, kind)
        if owner_view:
            result = _team_view(db, result)
        db.commit()
        return result


def publish(store, identity, request: Revision):
    request = Revision.model_validate(request.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _get(db, identity, "template", request.expected_revision, active=True)
        definition = Template.model_validate(current["definition"]).model_dump()
        if current["versions"] and current["versions"][-1]["definition"] == definition:
            raise agents.AgentError(
                "already_published", "Change the draft before publishing another version."
            )
        version, now = len(current["versions"]) + 1, time.time()
        db.execute(
            "INSERT INTO template_publications VALUES (?,?,?,?)",
            (identity, version, json.dumps(definition), now),
        )
        db.execute(
            "UPDATE collaboration_records SET revision=revision+1,updated_at=? WHERE id=?",
            (now, identity),
        )
        db.commit()
        return {"version": version, "definition": definition, "published_at": now}


def prepare(store, identity, kind, request):
    request = (Instantiate if kind == "template" else Revision).model_validate(request.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _get(db, identity, kind, request.expected_revision, active=True)
        result = {
            "id": "preparation_" + uuid.uuid4().hex,
            "created_at": time.time(),
            "status": "prepared",
            "authority": "none",
            "execution": "not-integrated",
            "reference_validation": "external-references-unverified",
            "kind": kind,
        }
        if kind == "template":
            version = next(
                (v for v in current["versions"] if v["version"] == request.version), None
            )
            if version is None:
                raise agents.AgentError(
                    "version_not_found", "Choose a published template version.", 404
                )
            try:
                configuration = agents.AgentInput.model_validate(
                    {**version["definition"]["agent"], **request.overrides, "name": request.name}
                )
                Template.limits(configuration)
            except (ValidationError, ValueError) as exc:
                raise agents.AgentError(
                    "invalid_configuration", "Provide valid whole-field agent overrides.", 422
                ) from exc
            agents._unique_name(db, configuration)
            result.update(
                templateId=identity,
                templateVersion=request.version,
                configuration=configuration.model_dump(),
                overriddenFields=["name", *request.overrides],
            )
        else:
            definition = Team.model_validate(current["definition"])
            result.update(
                teamId=identity,
                teamRevision=current["revision"],
                definition=definition.model_dump(),
                agents=_team_agents(db, definition),
            )
        db.execute(
            "INSERT INTO collaboration_preparations VALUES (?,?,?,?)",
            (result["id"], identity, kind, json.dumps(result)),
        )
        db.commit()
        return result


def remove(store, identity, kind, request: Revision):
    request = Revision.model_validate(request.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _get(db, identity, kind, request.expected_revision)
        if (kind == "template" and current["versions"]) or db.execute(
            "SELECT 1 FROM collaboration_preparations WHERE record_id=?", (identity,)
        ).fetchone():
            raise agents.AgentError(
                "referenced",
                "Archive this configuration to preserve published versions and preparations.",
            )
        now = time.time()
        db.execute(
            "UPDATE collaboration_records SET deleted_at=?,updated_at=?,revision=revision+1 WHERE id=?",
            (now, now, identity),
        )
        db.commit()
    return {"id": identity, "removed": True, "revision": current["revision"] + 1}
