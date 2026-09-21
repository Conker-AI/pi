"""Future-turn privacy and immutable execution selections; no permission grants."""
import json
from pydantic import Field
from . import agents


class Privacy(agents.StrictModel):
    memoryDisabled: bool
    harnessDisabled: bool


class Settings(agents.StrictModel):
    agentId: str = Field(min_length=1, max_length=200)
    privacy: Privacy
    projectId: str | None = Field(default=None, min_length=1, max_length=200)


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
for _table, _id in (("submission_settings", "request_id"), ("turn_settings", "turn_id"), ("message_privacy", "message_id")):
    for _op in ("UPDATE", "DELETE"):
        SCHEMA += f"CREATE TRIGGER IF NOT EXISTS {_table}_no_{_op.lower()} BEFORE {_op} ON {_table} BEGIN SELECT RAISE(ABORT,'execution selection is immutable'); END;\n"
    SCHEMA += f"CREATE TRIGGER IF NOT EXISTS {_table}_no_replace BEFORE INSERT ON {_table} WHEN EXISTS(SELECT 1 FROM {_table} WHERE {_id}=NEW.{_id}) BEGIN SELECT RAISE(ABORT,'execution selection is immutable'); END;\n"


def _load(db, identity):
    row = db.execute("SELECT status FROM sessions WHERE id=?", (identity,)).fetchone()
    if row is None or row[0] == "forgotten":
        raise agents.AgentError("not_found", "Conversation unavailable.", 404)
    stored = db.execute("SELECT * FROM session_settings WHERE session_id=?", (identity,)).fetchone()
    return {"revision": stored["revision"] if stored else 0,
            "settings": json.loads(stored["settings"]) if stored else json.loads(json.dumps(DEFAULT))}


def load(store, identity):
    with store._connect() as db:
        return _load(db, identity)


def _snapshot(db, identity):
    value = _load(db, identity)
    agent = agents._get(db, value["settings"]["agentId"])
    if agent["archived_at"] is not None:
        raise agents.AgentError("agent_archived", "Restore or select an active agent before starting work.")
    models = db.execute("SELECT revision,configuration FROM model_role_settings WHERE singleton=1").fetchone()
    project = _project(db, value["settings"].get("projectId"))
    return {**value, "agentId": agent["id"], "agentVersion": agent["revision"],
            "modelConfigurationRevision": models["revision"] if models else 0,
            "modelConfiguration": json.loads(models["configuration"]) if models else None,
            "configuration": agent["configuration"], "kind": agent["kind"],
            "privacy": value["settings"]["privacy"], "project": project, "authority": "none"}


def _project(db, identity):
    if identity is None:
        return None
    row = db.execute("SELECT revision,fields,archived_at FROM projects WHERE id=?", (identity,)).fetchone()
    if row is None or row["archived_at"] is not None:
        raise agents.AgentError("project_unavailable", "Select an active project before starting work.")
    return {"id": identity, "revision": row["revision"],
            "instructions": json.loads(row["fields"])["instructions"]}


def save(store, identity, body):
    body = Update.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _load(db, identity)
        if current["revision"] != body.expected_revision:
            raise agents.AgentError("revision_conflict", "Session settings changed.", current_revision=current["revision"])
        if db.execute("SELECT 1 FROM sessions WHERE id=? AND status!='open'", (identity,)).fetchone():
            raise agents.AgentError("closed", "Change settings on an open conversation.")
        if db.execute("SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'", (identity,)).fetchone() or db.execute(
                "SELECT 1 FROM turns WHERE session_id=? AND (status NOT IN ('complete','failed','interrupted') OR (status='interrupted' AND acted=1))", (identity,)).fetchone():
            raise agents.AgentError("session_busy", "Resolve current work before changing session settings.")
        agent = agents._get(db, body.settings.agentId)
        if agent["archived_at"] is not None:
            raise agents.AgentError("agent_archived", "Select an active agent.")
        _project(db, body.settings.projectId)
        db.execute("INSERT INTO session_settings VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,settings=excluded.settings",
                   (identity, current["revision"] + 1, body.settings.model_dump_json()))
        result = _load(db, identity)
        db.commit()
        return result


def reserve(db, request_id, identity):
    db.execute("INSERT INTO submission_settings VALUES (?,?)", (request_id, json.dumps(_snapshot(db, identity))))


def bind(db, turn_id, identity, request_id=None):
    row = db.execute("SELECT snapshot FROM submission_settings WHERE request_id=?", (request_id,)).fetchone() if request_id else None
    db.execute("INSERT INTO turn_settings VALUES (?,?)", (turn_id, row[0] if row else json.dumps(_snapshot(db, identity))))


def execution(store, identity, turn_id=None, request_id=None):
    with store._connect() as db:
        _load(db, identity)
        if turn_id or request_id:
            if turn_id and request_id:
                raise agents.AgentError("invalid_execution_identity", "Choose one execution identity.", 422)
            if turn_id:
                origin = db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)).fetchone()
            else:
                origin = db.execute("SELECT requested_session_id FROM turn_submissions WHERE request_id=?", (request_id,)).fetchone()
            if origin is None or origin[0] != identity:
                raise agents.AgentError("foreign_execution", "Execution does not belong to this conversation.", 404)
            table, key, value = ("turn_settings", "turn_id", turn_id) if turn_id else ("submission_settings", "request_id", request_id)
            row = db.execute(f"SELECT snapshot FROM {table} WHERE {key}=?", (value,)).fetchone()
            if row:
                return json.loads(row[0])
            # Legacy turns had only the Companion, without runtime privacy settings.
            return {"agentId": "companion", "kind": "companion", "configuration": None,
                    "privacy": DEFAULT["privacy"], "legacy": True}
        return _snapshot(db, identity)


def source_privacy(db, identity):
    try:
        privacy = _load(db, identity)["settings"]["privacy"].copy()
    except agents.AgentError:
        return None
    rows = db.execute("SELECT p.memory_disabled,p.harness_disabled FROM message_privacy p JOIN messages m ON m.id=p.message_id WHERE m.session_id=?", (identity,))
    for row in rows:
        privacy["memoryDisabled"] |= bool(row[0])
        privacy["harnessDisabled"] |= bool(row[1])
    return {**privacy, "incognito": any(privacy.values())}


def memory_allowed(snapshot):
    # Specialist namespace/authority mapping is deliberately not guessed.
    return not snapshot["privacy"]["memoryDisabled"] and snapshot["kind"] == "companion"
