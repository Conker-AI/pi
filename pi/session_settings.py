"""Future-turn privacy and immutable execution selections; no permission grants."""

import json
from typing import Literal

from pydantic import Field, model_validator

from . import agents, projects


class Privacy(agents.StrictModel):
    memoryDisabled: bool
    harnessDisabled: bool


class Settings(agents.StrictModel):
    agentId: str = Field(min_length=1, max_length=200)
    privacy: Privacy
    projectId: str | None = Field(default=None, min_length=1, max_length=200)
    projectSources: list[projects.Reference] = Field(default_factory=list, max_length=20)
    presentationMode: Literal["focus", "character"] | None = None

    @model_validator(mode="after")
    def project_sources(self):
        keys = [json.dumps(ref.model_dump(), sort_keys=True) for ref in self.projectSources]
        if len(keys) != len(set(keys)) or (keys and self.projectId is None):
            raise ValueError("Select distinct linked sources within a project.")
        return self


class Update(agents.StrictModel):
    expected_revision: int = Field(ge=0)
    settings: Settings


DEFAULT = {"agentId": "companion", "privacy": {"memoryDisabled": False, "harnessDisabled": False}}
SCHEMA = """
CREATE TABLE IF NOT EXISTS session_settings (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), revision INTEGER NOT NULL, settings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_settings (
 request_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_settings (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_privacy (
 message_id TEXT PRIMARY KEY REFERENCES messages(id),
 memory_disabled INTEGER NOT NULL, harness_disabled INTEGER NOT NULL, allow_ingest INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS inherit_session_settings AFTER INSERT ON sessions
WHEN NEW.parent_id IS NOT NULL
BEGIN
 INSERT INTO session_settings SELECT NEW.id,revision,settings FROM session_settings WHERE session_id=NEW.parent_id;
END;
"""
for _table, _id in (
    ("submission_settings", "request_id"),
    ("turn_settings", "turn_id"),
    ("message_privacy", "message_id"),
):
    for _op in ("UPDATE", "DELETE"):
        SCHEMA += f"CREATE TRIGGER IF NOT EXISTS {_table}_no_{_op.lower()} BEFORE {_op} ON {_table} BEGIN SELECT RAISE(ABORT,'execution selection is immutable'); END;\n"
    SCHEMA += f"CREATE TRIGGER IF NOT EXISTS {_table}_no_replace BEFORE INSERT ON {_table} WHEN EXISTS(SELECT 1 FROM {_table} WHERE {_id}=NEW.{_id}) BEGIN SELECT RAISE(ABORT,'execution selection is immutable'); END;\n"


def _load(db, identity):
    row = db.execute("SELECT status FROM sessions WHERE id=?", (identity,)).fetchone()
    if row is None or row[0] == "forgotten":
        raise agents.AgentError("not_found", "Conversation unavailable.", 404)
    stored = db.execute("SELECT * FROM session_settings WHERE session_id=?", (identity,)).fetchone()
    return {
        "revision": stored["revision"] if stored else 0,
        "settings": json.loads(stored["settings"]) if stored else json.loads(json.dumps(DEFAULT)),
    }


def load(store, identity):
    with store._connect() as db:
        return _load(db, identity)


def _snapshot(db, identity):
    from . import team_execution

    frozen = team_execution.session_snapshot(db, identity)
    if frozen is not None:
        return frozen
    from . import calls, characters, project_context

    value = _load(db, identity)
    agent = agents._get(db, value["settings"]["agentId"])
    if agent["archived_at"] is not None:
        raise agents.AgentError(
            "agent_archived", "Restore or select an active agent before starting work."
        )
    models = db.execute(
        "SELECT revision,configuration FROM model_role_settings WHERE singleton=1"
    ).fetchone()
    project = _project(db, value["settings"].get("projectId"))
    snapshot = {
        **value,
        "agentId": agent["id"],
        "agentVersion": agent["revision"],
        "modelConfigurationRevision": models["revision"] if models else 0,
        "modelConfiguration": json.loads(models["configuration"]) if models else None,
        "configuration": agent["configuration"],
        "kind": agent["kind"],
        "character": characters.runtime_snapshot(db, agent["id"]),
        "presentationMode": value["settings"].get("presentationMode"),
        "privacy": value["settings"]["privacy"],
        "project": project,
        "authority": "none",
        "projectContext": project_context.capture(db, identity, value["settings"]),
    }
    return calls.execution_snapshot(db, identity, snapshot)


def _project(db, identity):
    if identity is None:
        return None
    row = db.execute(
        "SELECT revision,fields,archived_at FROM projects WHERE id=?", (identity,)
    ).fetchone()
    if row is None or row["archived_at"] is not None:
        raise agents.AgentError(
            "project_unavailable", "Select an active project before starting work."
        )
    return {
        "id": identity,
        "revision": row["revision"],
        "instructions": json.loads(row["fields"])["instructions"],
    }


def save(store, identity, body):
    body = Update.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _load(db, identity)
        if db.execute("SELECT 1 FROM calls WHERE session_id=?", (identity,)).fetchone():
            raise agents.AgentError(
                "call_settings", "Change call preferences through the call controls."
            )
        if current["revision"] != body.expected_revision:
            raise agents.AgentError(
                "revision_conflict",
                "Session settings changed.",
                current_revision=current["revision"],
            )
        if db.execute(
            "SELECT 1 FROM sessions WHERE id=? AND status!='open'", (identity,)
        ).fetchone():
            raise agents.AgentError("closed", "Change settings on an open conversation.")
        if (
            db.execute(
                "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                (identity,),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM turns WHERE session_id=? AND (status NOT IN ('complete','failed','interrupted','cancelled') OR (status='interrupted' AND acted=1))",
                (identity,),
            ).fetchone()
        ):
            raise agents.AgentError(
                "session_busy", "Resolve current work before changing session settings."
            )
        inherited = db.execute(
            "SELECT MAX(p.memory_disabled),MAX(p.harness_disabled) "
            "FROM context_inherited_messages i JOIN message_privacy p ON p.message_id=i.message_id "
            "WHERE i.session_id=?",
            (identity,),
        ).fetchone()
        if (inherited[0] and not body.settings.privacy.memoryDisabled) or (
            inherited[1] and not body.settings.privacy.harnessDisabled
        ):
            raise agents.AgentError(
                "inherited_privacy", "Keep the inherited messages' privacy modes enabled."
            )
        agent = agents._get(db, body.settings.agentId)
        if agent["archived_at"] is not None:
            raise agents.AgentError("agent_archived", "Select an active agent.")
        _project(db, body.settings.projectId)
        db.execute(
            "INSERT INTO session_settings VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,settings=excluded.settings",
            (identity, current["revision"] + 1, body.settings.model_dump_json()),
        )
        result = _load(db, identity)
        db.commit()
        return result


def validate_answer_model(snapshot, model_id):
    if model_id is None:
        return
    if snapshot.get("callExecution") or snapshot["kind"] == "team-role":
        raise agents.AgentError("model_scope", "Use the call or team model controls.", 422)
    from . import model_roles

    raw = snapshot.get("modelConfiguration")
    if not raw:
        raise agents.AgentError("model_unconfigured", "Configure the model catalogue first.", 422)
    config = model_roles.Configuration.model_validate(raw)
    answer = config.roleSettings.roles["answer"]
    if (
        not answer.enabled
        or not isinstance(model_id, str)
        or model_id not in answer.eligibleModelIds
        or not any(
            m.id == model_id
            and m.enabled
            and any(p.id == m.providerId and p.enabled for p in config.providers)
            for m in config.models
        )
    ):
        raise agents.AgentError(
            "model_unavailable", "Choose an enabled, eligible answer model.", 422
        )


def validate_reply(db, identity, reply_to):
    if reply_to is None:
        return
    row = db.execute(
        "SELECT id FROM messages WHERE id=? AND role IN ('user','assistant') "
        "AND session_id NOT IN (SELECT session_id FROM forgotten_sessions) "
        "AND (session_id=? OR id IN (SELECT message_id FROM context_inherited_messages "
        "WHERE session_id=?))",
        (reply_to, identity, identity),
    ).fetchone()
    if not row:
        raise agents.AgentError(
            "reply_unavailable", "Choose an available message in this conversation.", 422
        )
    policy = db.execute(
        "SELECT policy FROM context_policies WHERE session_id=?", (identity,)
    ).fetchone()
    if policy and json.loads(policy[0])["messagePolicies"].get(reply_to) == "exclude":
        raise agents.AgentError(
            "reply_excluded", "Include this message in context before replying.", 422
        )


def reserve(db, request_id, identity, model_id=None, reply_to=None, research_mode="off"):
    from . import project_context

    snapshot = _snapshot(db, identity)
    from . import research

    research.validate(research_mode, snapshot)
    if research_mode != "off":
        snapshot["researchMode"] = research_mode
    validate_answer_model(snapshot, model_id)
    validate_reply(db, identity, reply_to)
    if reply_to is not None:
        snapshot["replyToMessageId"] = reply_to
    if model_id is not None:
        snapshot["answerModelId"] = model_id
    db.execute("INSERT INTO submission_settings VALUES (?,?)", (request_id, json.dumps(snapshot)))
    project_context.record_dependencies(db, identity, snapshot)


def bind(db, turn_id, identity, request_id=None):
    from . import project_context

    row = (
        db.execute(
            "SELECT snapshot FROM submission_settings WHERE request_id=?", (request_id,)
        ).fetchone()
        if request_id
        else None
    )
    snapshot = json.loads(row[0]) if row else _snapshot(db, identity)
    db.execute("INSERT INTO turn_settings VALUES (?,?)", (turn_id, json.dumps(snapshot)))
    project_context.record_dependencies(db, identity, snapshot)


def execution(store, identity, turn_id=None, request_id=None):
    with store._connect() as db:
        _load(db, identity)
        if turn_id or request_id:
            if turn_id and request_id:
                raise agents.AgentError(
                    "invalid_execution_identity", "Choose one execution identity.", 422
                )
            if turn_id:
                origin = db.execute(
                    "SELECT session_id FROM turns WHERE id=?", (turn_id,)
                ).fetchone()
            else:
                origin = db.execute(
                    "SELECT requested_session_id FROM turn_submissions WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            if origin is None or origin[0] != identity:
                raise agents.AgentError(
                    "foreign_execution", "Execution does not belong to this conversation.", 404
                )
            table, key, value = (
                ("turn_settings", "turn_id", turn_id)
                if turn_id
                else ("submission_settings", "request_id", request_id)
            )
            row = db.execute(f"SELECT snapshot FROM {table} WHERE {key}=?", (value,)).fetchone()
            if row:
                snapshot = json.loads(row[0])
                if turn_id:
                    snapshot["turnExecutionId"] = turn_id
                if request_id:
                    snapshot["submissionExecutionId"] = request_id
                return snapshot
            # Legacy turns had only the Companion, without runtime privacy settings.
            return {
                "agentId": "companion",
                "kind": "companion",
                "configuration": None,
                "privacy": DEFAULT["privacy"],
                "legacy": True,
                "turnExecutionId": turn_id,
            }
        return _snapshot(db, identity)


def source_privacy(db, identity):
    try:
        privacy = _load(db, identity)["settings"]["privacy"].copy()
    except agents.AgentError:
        return None
    rows = db.execute(
        "SELECT p.memory_disabled,p.harness_disabled FROM message_privacy p JOIN messages m ON m.id=p.message_id WHERE m.session_id=?",
        (identity,),
    )
    for row in rows:
        privacy["memoryDisabled"] |= bool(row[0])
        privacy["harnessDisabled"] |= bool(row[1])
    return {**privacy, "incognito": any(privacy.values())}


def memory_allowed(snapshot):
    if snapshot["kind"] == "team-role":
        read = snapshot.get("memoryRead", {})
        return (
            read.get("disabled") is False
            and bool(read.get("sourceSessionId"))
            and snapshot["configuration"]["memory"]["scope"] != "none"
        )
    # Retrieval authority is resolved separately by Memory's operator-owned map.
    return not snapshot["privacy"]["memoryDisabled"] and snapshot["kind"] in ("companion", "agent")
