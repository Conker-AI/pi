"""Team run reads, shared row/session helpers and the plain state transitions (a pure move out of
team_execution.py to keep that file under the size limit; the spending checks stay there)."""

import hashlib
import json
import time
import uuid
from typing import NoReturn

from pi.agents import profiles as agents
from pi.agents.team_models import Finish
from pi.conversation import settings as session_settings
from pi.conversation import visibility
from pi.schedule import tasks

LIMITS = {
    "turnsAndHandoffs": "strict",
    "tokensAndCost": "observed-stop; the current provider call may overshoot",
    "unknownUsage": "blocks further calls",
    "toolCharges": "separate ToolGate authorization; not included in model cost",
    "conditions": "owner-reviewed, not automatically evaluated",
    "authority": "none",
}


def _error(code, message, status=409) -> NoReturn:
    raise agents.AgentError(code, message, status)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _row(db, identity):
    row = db.execute("SELECT * FROM team_runs WHERE id=?", (identity,)).fetchone()
    if row is None:
        _error("not_found", "Team run not found.", 404)
    tasks._source(db, row["root_session_id"], open_required=True)
    return row


def _session(db, parent, privacy):
    identity = "ses_" + uuid.uuid4().hex[:16]
    db.execute(
        "INSERT INTO sessions(id,parent_id,title,created_at) VALUES(?,?,?,?)",
        (identity, parent, "Team execution", time.time()),
    )
    session_settings.inherited_privacy(db, identity)
    # Explicitly narrowed privacy prevents team copies entering Companion ingestion.
    settings = {"agentId": "companion", "privacy": {**privacy, "memoryDisabled": True}}
    db.execute(
        "INSERT INTO session_settings VALUES (?,1,?) ON CONFLICT(session_id) "
        "DO UPDATE SET revision=1,settings=excluded.settings",
        (identity, json.dumps(settings)),
    )
    return identity


def _source(db, identity, session_id):
    privacy = session_settings.source_privacy(db, session_id)
    if privacy is None or privacy["incognito"] or visibility.hidden(db, [session_id]):
        _error("private_source", "Select available messages without privacy exclusions.")
    row = db.execute(
        "SELECT m.*,p.memory_disabled,p.harness_disabled FROM messages m "
        "JOIN message_privacy p ON p.message_id=m.id "
        "JOIN sessions s ON s.id=m.session_id "
        "WHERE m.id=? AND m.session_id=? AND s.status!='forgotten'",
        (identity, session_id),
    ).fetchone()
    if row is None or row["memory_disabled"] or row["harness_disabled"]:
        _error("private_source", "Select available messages without privacy exclusions.")
    return row


def get(store, identity):
    with store._connect() as db:
        row = _row(db, identity)
        steps = [
            dict(value)
            for value in db.execute(
                "SELECT id,request_id,role_id,handoff_id,session_id,task_id,state,created_at "
                "FROM team_steps WHERE run_id=? ORDER BY rowid",
                (identity,),
            )
        ]
        calls = [
            dict(value)
            for value in db.execute(
                "SELECT c.* FROM team_model_calls c JOIN team_steps s ON s.id=c.step_id "
                "WHERE s.run_id=? ORDER BY c.rowid",
                (identity,),
            )
        ]
        return {
            "id": identity,
            "teamId": row["team_id"],
            "revision": row["revision"],
            "state": row["state"],
            "rootSessionId": row["root_session_id"],
            "snapshot": json.loads(row["snapshot"]),
            "steps": steps,
            "modelCalls": calls,
            "limits": LIMITS,
        }


def session_snapshot(db, identity):
    row = db.execute("SELECT snapshot FROM team_steps WHERE session_id=?", (identity,)).fetchone()
    return json.loads(row[0]) if row else None


MAX_INPUT_CHARS = 16000  # a role's input is sent whole or not at all


def _role_input(store, run, definition, role, edge, previous_message) -> str:
    """The role's whole input, checked before the step is saved so a refusal leaves the run ready."""
    text = {
        "objective": definition["objective"],
        "ownerTask": store.get_message(run["input_message_id"])["content"],
        "selectedContext": [],
        "handoff": None,
    }
    for source in role["context"]["sourceIds"]:
        message = store.get_message(source)
        if not message or message.get("content_status") == "forgotten":
            _error("source_unavailable", "A selected message is unavailable.")
        text["selectedContext"].append({"id": source, "text": message["content"]})
    if previous_message:
        message = store.get_message(previous_message)
        if not message or message.get("content_status") == "forgotten":
            _error("source_unavailable", "The handoff result is unavailable.")
        text["handoff"] = {"messageId": previous_message, "result": message["content"]}
        if edge["payload"] == "result_and_citations":
            text["handoff"]["citations"] = message.get("citations", [])
    prompt = json.dumps(text, ensure_ascii=False)
    if len(prompt) > MAX_INPUT_CHARS:
        _error("input_limit", "Selected input exceeds 16000 characters; nothing was truncated.")
    return prompt


def mark_blocked(store, identity, step_id) -> None:
    """A step that could not run or finish: the step and its run stop (only while running)."""
    with store._connect() as db:
        db.execute(
            "UPDATE team_steps SET state='blocked' WHERE id=? AND state='running'", (step_id,)
        )
        db.execute(
            "UPDATE team_runs SET state='blocked' WHERE id=? AND state='running'", (identity,)
        )


def step_exists(store, identity, request_id) -> bool:
    with store._connect() as db:
        return (
            db.execute(
                "SELECT 1 FROM team_steps WHERE run_id=? AND request_id=?", (identity, request_id)
            ).fetchone()
            is not None
        )


def recover_interrupted(store):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE team_model_calls SET state='unknown' WHERE state='running'")
        db.execute(
            "UPDATE team_runs SET state='interrupted',revision=revision+1 WHERE state='running'"
        )
        changed = db.execute(
            "UPDATE team_steps SET state='interrupted' WHERE state='running'"
        ).rowcount
        db.commit()
        return changed


def finish(store, identity, body):
    body = Finish.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        run = _row(db, identity)
        if run["revision"] != body.expected_revision:
            _error("revision_conflict", "Reload the team run before finishing.")
        # An interrupted run's outcome stays unknown for good: the owner can only cancel it.
        if run["state"] in ("running", "held") or (
            run["state"] == "interrupted" and body.state != "cancelled"
        ):
            _error("unresolved_run", "Resolve pending or uncertain work before finishing.")
        if body.state == "completed" and run["state"] != "ready":
            _error("blocked_run", "Blocked work cannot be marked completed.")
        db.execute(
            "UPDATE team_runs SET state=?,revision=revision+1 WHERE id=?", (body.state, identity)
        )
        db.commit()
    return get(store, identity)
