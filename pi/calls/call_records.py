"""The call record: its error, rows, events, liveness and owner view."""

from __future__ import annotations

import hashlib
import json
import time

from pi.conversation import settings as session_settings
from pi.core.errors import ServiceError


class CallError(ServiceError, RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _row(db, identity, revision=None):
    row = db.execute("SELECT * FROM calls WHERE id=?", (identity,)).fetchone()
    if row is None or row["forgotten"]:
        raise CallError("not_found", "Call unavailable.", 404)
    if revision is not None and row["revision"] != revision:
        raise CallError("revision_conflict", "Call changed; reload before updating.")
    return row


def _event(db, row, kind):
    db.execute(
        "INSERT INTO call_events(call_id,kind,at,generation) VALUES(?,?,?,?)",
        (row["id"], kind, time.time(), row["generation"]),
    )


def _live(db, row, generation=None):
    if (
        row["ended_at"]
        or row["paused"]
        or row["forgotten"]
        or (generation is not None and generation != row["generation"])
    ):
        raise CallError("interrupted", "Call was paused, ended or interrupted.")
    source = db.execute(
        "SELECT summary,status FROM sessions WHERE id=?", (row["source_session_id"],)
    ).fetchone()
    child = db.execute("SELECT status FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
    if not source or source["status"] == "forgotten" or not child or child[0] != "open":
        raise CallError("source_unavailable", "Call conversation is unavailable.")
    if _digest((source["summary"] or "").encode()) != row["summary_hash"]:
        raise CallError("source_changed", "Earlier conversation summary changed; start a new call.")
    privacy = session_settings.source_privacy(db, row["source_session_id"])
    for origin in db.execute(
        "SELECT DISTINCT m.session_id FROM messages m JOIN context_inherited_messages i "
        "ON i.message_id=m.id WHERE i.session_id=?",
        (row["session_id"],),
    ):
        inherited = session_settings.source_privacy(db, origin[0])
        if inherited is None:
            raise CallError("privacy_changed", "Referenced conversation privacy is unavailable.")
        if privacy is not None:
            privacy = {key: privacy[key] or inherited[key] for key in privacy}
    selected = json.loads(row["settings"])["privacy"]
    if (
        privacy is None
        or (privacy["memoryDisabled"] and not selected["memory"])
        or (privacy["harnessDisabled"] and not selected["harness"])
    ):
        raise CallError("privacy_changed", "Call privacy must preserve the source exclusions.")


def _view(db, row):
    settings = json.loads(row["settings"])
    agent = session_settings._load(db, row["session_id"])["settings"]["agentId"]
    events = [
        {"id": f"call_event_{r['id']}", "at": r["at"], "kind": "event", "text": r["kind"]}
        for r in db.execute("SELECT * FROM call_events WHERE call_id=? ORDER BY id", (row["id"],))
    ]
    requests = []
    for request in db.execute(
        "SELECT * FROM call_requests WHERE call_id=? ORDER BY created_at,request_id", (row["id"],)
    ):
        submission = db.execute(
            "SELECT s.turn_id,s.input_message_id,t.status,t.acted FROM turn_submissions "
            "s LEFT JOIN turns t ON t.id=s.turn_id WHERE s.request_id=?",
            (request["request_id"],),
        ).fetchone()
        references = []
        if submission:
            refs = db.execute(
                "SELECT m.id,m.role,m.content,m.created_at FROM messages m JOIN "
                "turn_messages tm ON tm.message_id=m.id WHERE tm.turn_id=? AND "
                "tm.purpose IN ('input','final') ORDER BY m.seq",
                (submission["turn_id"],),
            ).fetchall()
            for message in refs:
                references.append(message["id"])
                events.append(
                    {
                        "id": message["id"],
                        "at": message["created_at"],
                        "kind": "user" if message["role"] == "user" else "assistant",
                        "text": json.loads(message["content"]),
                        "source": {"sessionId": row["session_id"], "messageId": message["id"]},
                    }
                )
        requests.append(
            {
                "requestId": request["request_id"],
                "state": request["state"],
                "inputKind": request["input_kind"],
                "speechStatus": request["speech_status"],
                "errorCode": request["error_code"],
                "turnId": submission["turn_id"] if submission else None,
                "turnStatus": submission["status"] if submission else None,
                "textStatus": "complete"
                if submission and submission["status"] == "complete"
                else "not-complete",
                "acted": bool(submission["acted"]) if submission else False,
                "messageIds": references,
            }
        )
    return {
        "id": row["id"],
        "conversationId": row["source_session_id"],
        "sessionId": row["session_id"],
        "agentId": agent,
        "name": "Call",
        "startedAt": row["started_at"],
        "endedAt": row["ended_at"],
        "revision": row["revision"],
        "generation": row["generation"],
        "phase": row["phase"],
        "paused": bool(row["paused"]),
        **settings,
        "events": sorted(events, key=lambda e: (e["at"], e["id"])),
        "requests": requests,
        "capabilities": {
            "typedTurns": True,
            "speech": "adapter-dependent",
            "camera": "unavailable",
            "perception": "unavailable",
            "emotion": "unavailable",
            "characterVoice": "adapter-dependent; inspect /calls/capabilities",
            "channels": "preferences; no device capture",
            "rawMediaRetention": "none",
            "audioReplay": "unavailable",
            "interruption": "cooperative; in-flight effects cannot be recalled",
        },
    }


def get(store, identity):
    with store._connect() as db:
        db.execute("BEGIN")
        return _view(db, _row(db, identity))
