"""Stored templates and bounded team preparations; never a dispatch or grant."""

import json
import time
import uuid

from pydantic import ValidationError

from pi.agents import profiles as agents
from pi.agents.team_models import (
    AgentReference,
    Instantiate,
    Revision,
    Team,
    TeamCollection,
    TeamHistory,
    TeamRevision,
    TeamSummary,
    TeamView,
    Template,
)

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
