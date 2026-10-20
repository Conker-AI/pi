"""Durable conversation calls; real Loop turns, transient optional speech, no perception."""

from __future__ import annotations

import json
import time
import uuid

from pi.agents import profiles as agents
from pi.calls.call_models import Channels, Revision, Start, Update
from pi.calls.call_records import CallError, _digest, _event, _live, _row, _view
from pi.context import controls as context_controls
from pi.conversation import settings as session_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
 id TEXT PRIMARY KEY, start_request_id TEXT NOT NULL UNIQUE, start_hash TEXT,
 source_session_id TEXT NOT NULL REFERENCES sessions(id),
 session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id), revision INTEGER NOT NULL,
 generation INTEGER NOT NULL, started_at REAL NOT NULL, ended_at REAL,
 paused INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL,
 settings TEXT NOT NULL, summary_hash TEXT, forgotten INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS calls_one_active_source ON calls(source_session_id)
WHERE ended_at IS NULL AND forgotten=0;
CREATE TABLE IF NOT EXISTS call_requests (
 request_id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
 generation INTEGER NOT NULL, payload_hash TEXT, input_kind TEXT NOT NULL,
 state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 preferences TEXT, error_code TEXT, speech_status TEXT NOT NULL DEFAULT 'not-requested'
);
CREATE UNIQUE INDEX IF NOT EXISTS calls_one_running_request ON call_requests(call_id)
WHERE state IN ('reserved','transcribing','model','synthesizing');
CREATE TABLE IF NOT EXISTS call_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL REFERENCES calls(id),
 kind TEXT NOT NULL, at REAL NOT NULL, generation INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS calls_submission_identity BEFORE INSERT ON turn_submissions
WHEN EXISTS(SELECT 1 FROM calls c WHERE c.session_id=NEW.requested_session_id)
AND NOT EXISTS(SELECT 1 FROM calls c JOIN call_requests r ON r.call_id=c.id
 WHERE c.session_id=NEW.requested_session_id AND r.request_id=NEW.request_id
 AND r.state='model' AND r.generation=c.generation AND c.paused=0
 AND c.ended_at IS NULL AND c.forgotten=0)
BEGIN SELECT RAISE(ABORT,'call turns require their active reserved request'); END;
CREATE TRIGGER IF NOT EXISTS call_requests_no_replace BEFORE INSERT ON call_requests
WHEN EXISTS(SELECT 1 FROM call_requests WHERE request_id=NEW.request_id)
BEGIN SELECT RAISE(ABORT,'call request identities are immutable'); END;
CREATE TRIGGER IF NOT EXISTS call_requests_fixed BEFORE UPDATE ON call_requests
WHEN NEW.request_id!=OLD.request_id OR NEW.call_id!=OLD.call_id
 OR NEW.generation!=OLD.generation OR NEW.input_kind!=OLD.input_kind
 OR NEW.created_at!=OLD.created_at
 OR (NEW.preferences IS NOT OLD.preferences AND NEW.preferences IS NOT NULL)
 OR (NEW.payload_hash IS NOT OLD.payload_hash AND NEW.payload_hash IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'call request identities are immutable'); END;
CREATE TRIGGER IF NOT EXISTS call_requests_no_delete BEFORE DELETE ON call_requests
BEGIN SELECT RAISE(ABORT,'call request receipts are permanent'); END;
"""


def start(store, body: Start):
    body = Start.model_validate(body.model_dump())
    digest = _digest(body.model_dump_json().encode())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT * FROM calls WHERE start_request_id=?", (body.request_id,)
        ).fetchone()
        if prior:
            if prior["forgotten"] or prior["start_hash"] != digest:
                raise CallError("request_conflict", "Call start request cannot be reused.")
            return _view(db, prior)
        source = db.execute("SELECT * FROM sessions WHERE id=?", (body.conversationId,)).fetchone()
        if source is None or source["status"] != "open":
            raise CallError("source_unavailable", "Start a call from an active conversation.")
        if db.execute(
            "SELECT 1 FROM calls WHERE source_session_id=? AND ended_at IS NULL AND forgotten=0",
            (body.conversationId,),
        ).fetchone():
            raise CallError("call_active", "Reconnect to the existing call first.")
        if (
            db.execute("SELECT 1 FROM calls WHERE session_id=?", (body.conversationId,)).fetchone()
            or db.execute(
                "SELECT 1 FROM team_steps WHERE session_id=?", (body.conversationId,)
            ).fetchone()
        ):
            raise CallError(
                "source_boundary", "Calls require an ordinary conversation, not an execution child."
            )
        privacy = session_settings.source_privacy(db, body.conversationId)
        if privacy is None:
            raise CallError("privacy_unknown", "Source privacy is unavailable.")
        selected = session_settings._load(db, body.conversationId)["settings"]
        agent = agents._get(db, selected["agentId"])
        if agent["archived_at"]:
            raise CallError("agent_unavailable", "Select an active source agent.")
        flags = {
            "memory": privacy["memoryDisabled"] or body.privacy.memory,
            "harness": privacy["harnessDisabled"] or body.privacy.harness,
        }
        selected["privacy"] = {
            "memoryDisabled": flags["memory"],
            "harnessDisabled": flags["harness"],
        }
        identity, child, now = (
            "call_" + uuid.uuid4().hex,
            "ses_" + uuid.uuid4().hex[:16],
            time.time(),
        )
        db.execute(
            "INSERT INTO sessions(id,parent_id,title,status,created_at) VALUES(?,?,?,'open',?)",
            (child, body.conversationId, "Call", now),
        )
        session_settings.inherited_privacy(db, child)
        db.execute(
            "INSERT INTO session_settings VALUES(?,1,?) ON CONFLICT(session_id) DO "
            "UPDATE SET revision=1,settings=excluded.settings",
            (child, json.dumps(selected)),
        )
        policy = db.execute(
            "SELECT policy FROM context_policies WHERE session_id=?", (body.conversationId,)
        ).fetchone()
        if policy:
            context_controls.Policy.model_validate_json(policy[0])
            db.execute("INSERT INTO context_policies VALUES(?,1,?)", (child, policy[0]))
        inherited = [
            r[0]
            for r in db.execute(
                "SELECT message_id FROM context_inherited_messages WHERE session_id=? "
                "ORDER BY position",
                (body.conversationId,),
            )
        ]
        own = [
            r[0]
            for r in db.execute(
                "SELECT id FROM messages WHERE session_id=? ORDER BY seq", (body.conversationId,)
            )
        ]
        for position, message_id in enumerate(dict.fromkeys([*inherited, *own])):
            origin = db.execute(
                "SELECT session_id FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            inherited_privacy = session_settings.source_privacy(db, origin[0]) if origin else None
            if inherited_privacy is None:
                raise CallError("privacy_unknown", "Referenced source privacy is unavailable.")
            flags["memory"] |= inherited_privacy["memoryDisabled"]
            flags["harness"] |= inherited_privacy["harnessDisabled"]
            db.execute(
                "INSERT INTO context_inherited_messages VALUES(?,?,?)",
                (child, message_id, position),
            )
        selected["privacy"] = {
            "memoryDisabled": flags["memory"],
            "harnessDisabled": flags["harness"],
        }
        db.execute(
            "UPDATE session_settings SET settings=? WHERE session_id=?",
            (json.dumps(selected), child),
        )
        from pi.agents import character_context, characters

        presentation = {
            "presentationMode": selected.get("presentationMode"),
            "character": characters.runtime_snapshot(db, selected["agentId"]),
        }
        settings = {
            "channels": Channels().model_dump(),
            "mode": character_context.mode(presentation),
            "modelId": None,
            "privacy": flags,
        }
        db.execute(
            "INSERT INTO calls VALUES(?,?,?,?,?,1,1,?,NULL,0,'ready',?,?,0)",
            (
                identity,
                body.request_id,
                digest,
                body.conversationId,
                child,
                now,
                json.dumps(settings),
                _digest((source["summary"] or "").encode()),
            ),
        )
        row = _row(db, identity)
        _event(db, row, "Call started · English; no recording")
        result = _view(db, row)
        db.commit()
        return result


def active(store, conversation_id):
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute(
            "SELECT * FROM calls WHERE source_session_id=? AND ended_at IS NULL "
            "AND forgotten=0 ORDER BY started_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise CallError("not_found", "No active call is available for this conversation.", 404)
        return _view(db, row)


def listing(store, conversation_id):
    with store._connect() as db:
        db.execute("BEGIN")
        return [
            _view(db, row)
            for row in db.execute(
                "SELECT * FROM calls WHERE source_session_id=? AND forgotten=0 ORDER BY started_at",
                (conversation_id,),
            )
        ]


def update(store, identity, body: Update):
    body = Update.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity, body.expected_revision)
        if row["ended_at"]:
            raise CallError("ended", "Start a new call after ending.")
        settings = json.loads(row["settings"])
        stop = False
        if body.channels:
            patch = body.channels.model_dump(exclude_none=True)
            if patch.get("camera"):
                raise CallError(
                    "camera_unavailable", "Camera and perception are not implemented.", 422
                )
            stop = any(
                settings["channels"][key] and patch.get(key) is False
                for key in ("voice", "microphone")
            )
            settings["channels"].update(patch)
        if body.mode:
            settings["mode"] = body.mode
        if "modelId" in body.model_fields_set:
            if body.modelId:
                models = db.execute(
                    "SELECT configuration FROM model_role_settings WHERE singleton=1"
                ).fetchone()
                configured = json.loads(models[0]) if models else {}
                providers = {p["id"] for p in configured.get("providers", []) if p["enabled"]}
                if not any(
                    m["id"] == body.modelId and m["enabled"] and m["providerId"] in providers
                    for m in configured.get("models", [])
                ):
                    raise CallError("model_unavailable", "Choose an enabled configured model.", 422)
            settings["modelId"] = body.modelId
        if body.privacy:
            flags = body.privacy.model_dump()
            if any(settings["privacy"][key] and not flags[key] for key in flags):
                raise CallError(
                    "privacy_narrowing", "Call privacy exclusions cannot be relaxed.", 422
                )
            stop |= flags != settings["privacy"]
            settings["privacy"] = flags
            child = session_settings._load(db, row["session_id"])["settings"]
            child["privacy"] = {
                "memoryDisabled": flags["memory"],
                "harnessDisabled": flags["harness"],
            }
            db.execute(
                "UPDATE session_settings SET revision=revision+1,settings=? WHERE session_id=?",
                (json.dumps(child), row["session_id"]),
            )
        paused = bool(row["paused"]) if body.paused is None else body.paused
        stop |= paused != bool(row["paused"])
        db.execute(
            "UPDATE calls SET "
            "settings=?,paused=?,generation=generation+?,revision=revision+1 WHERE id=?",
            (json.dumps(settings), int(paused), int(stop), identity),
        )
        changed = _row(db, identity)
        _event(
            db,
            changed,
            "Call preferences updated"
            if body.paused is None
            else "Call paused"
            if paused
            else "Call resumed",
        )
        result = _view(db, changed)
        db.commit()
        return result


def interrupt(store, identity, body: Revision, *, ended=False):
    body = Revision.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity, body.expected_revision)
        if row["ended_at"]:
            return _view(db, row)
        db.execute(
            "UPDATE calls SET "
            "generation=generation+1,revision=revision+1,ended_at=?,phase=? WHERE id=?",
            (time.time() if ended else None, "ended" if ended else row["phase"], identity),
        )
        row = _row(db, identity)
        _event(
            db,
            row,
            "Call ended" if ended else "Response interrupted; in-flight effects may still finish",
        )
        result = _view(db, row)
        db.commit()
        return result


def execution_snapshot(db, session_id, base):
    row = db.execute("SELECT * FROM calls WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        return base
    _live(db, row)
    value = json.loads(json.dumps(base))
    active = db.execute(
        "SELECT preferences FROM call_requests WHERE call_id=? "
        "AND state IN ('reserved','transcribing','model','synthesizing')",
        (row["id"],),
    ).fetchone()
    settings = json.loads(active[0] if active and active[0] else row["settings"])
    if "character" in settings:
        value["character"] = settings["character"]
    value["callExecution"] = {"id": row["id"], "generation": row["generation"]}
    value["presentationMode"] = settings["mode"]
    value["privacy"] = {
        "memoryDisabled": settings["privacy"]["memory"],
        "harnessDisabled": settings["privacy"]["harness"],
    }
    if settings["modelId"]:
        value["configuration"]["modelId"] = settings["modelId"]
    return value


def recover(store):
    """Startup only: uncertain work never restarts and calls require explicit resume."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE call_requests SET "
            "state='unknown',error_code='process-restarted',updated_at=? WHERE state IN "
            "('reserved','transcribing','model','synthesizing')",
            (time.time(),),
        ).rowcount
        db.execute(
            "UPDATE calls SET "
            "paused=1,generation=generation+1,revision=revision+1,phase='ready' WHERE "
            "ended_at IS NULL AND forgotten=0"
        )
        db.commit()
        return changed


def redact(db, session_ids):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='calls'").fetchone():
        return
    for session_id in session_ids:
        db.execute(
            "UPDATE calls SET "
            "forgotten=1,paused=1,generation=generation+1,start_hash=NULL,summary_hash=NULL,"
            "settings='{}',ended_at=COALESCE(ended_at,?),phase='ended' "
            "WHERE (session_id=? OR source_session_id=?) AND forgotten=0",
            (time.time(), session_id, session_id),
        )
        db.execute(
            "UPDATE call_requests SET "
            "payload_hash=NULL,preferences=NULL,state='forgotten',error_code=NULL WHERE "
            "call_id IN (SELECT id FROM calls WHERE forgotten=1)"
        )
