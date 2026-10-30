"""Content-free, source-bound memory acknowledgement projections."""

import hashlib
import json
import math
import re
import unicodedata
from uuid import NAMESPACE_URL, uuid5

from pi.memory import store as memory_store

MAX_RECEIPT_BYTES = 16384
MAX_RESULTS = 100
NOTE_PREFIX = "Memory delivery acknowledgement for the current input: "
_METHODS = {"owner_requested", "signal", "filtered"}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def destination_digest(base):
    """Same destination identity as the dedicated correction client, without credentials."""
    return hashlib.sha256(
        json.dumps(
            {
                "scheme": base.scheme,
                "host": base.raw_host.decode("ascii"),
                "port": base.port,
                "path": base.raw_path.split(b"?", 1)[0].rstrip(b"/").decode("ascii"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def validate_receipt(value, message_id, agent_id):
    """A transport success or plausible score alone cannot acknowledge a memory save."""
    if not isinstance(value, dict) or not isinstance(agent_id, str) or not agent_id:
        raise ValueError("MemoryGate returned an invalid acknowledgement.")
    receipt_id = str(uuid5(NAMESPACE_URL, f"pi:{agent_id}:{message_id}"))
    memory_id = str(uuid5(NAMESPACE_URL, receipt_id + ":memory"))
    state, method = value.get("state"), value.get("admission_method")
    score = value.get("value_score")
    valid_score = score is None or (
        type(score) in (int, float) and 0 <= score <= 1 and math.isfinite(score)
    )
    if (
        value.get("id") != receipt_id
        or value.get("message_id") != message_id
        or not isinstance(state, str)
        or state not in {"admitted", "filtered", "deleted"}
        or (method is not None and (not isinstance(method, str) or method not in _METHODS))
        or not valid_score
        or (state == "admitted" and (value.get("memory_id") != memory_id or method == "filtered"))
        or (
            state == "filtered"
            and (value.get("memory_id") is not None or method not in {None, "filtered"})
        )
        or (state == "deleted" and value.get("memory_id") not in (None, memory_id))
    ):
        raise ValueError("MemoryGate returned an invalid acknowledgement.")
    return {
        "id": receipt_id,
        "message_id": message_id,
        "state": state,
        "memory_id": value.get("memory_id"),
        "value_score": score,
        "admission_method": method,
    }


def _view(state, reason=None, *, method=None, memory_id=None):
    return {
        "state": state,
        "reason_code": reason,
        "admission_method": method,
        "memory_id": memory_id,
    }


def for_message(db, message):
    if message["role"] != "user":
        return None
    if (
        message.get("content_status") == "forgotten"
        or db.execute(
            "SELECT 1 FROM forgotten_sessions WHERE session_id=?", (message["session_id"],)
        ).fetchone()
    ):
        return _view("removed", "forgotten")
    row = db.execute(
        "SELECT p.memory_disabled,p.allow_ingest,o.* FROM message_privacy p "
        "LEFT JOIN memory_outbox o ON o.message_id=p.message_id WHERE p.message_id=?",
        (message["id"],),
    ).fetchone()
    if row is None:
        return _view("unavailable", "legacy_unknown")
    if row["memory_disabled"] == 1:
        return _view("excluded", "privacy_disabled")
    if (row["allow_ingest"] == 0 or row["message_id"] is None) and memory_store.job_run_input(
        db, message["id"]
    ):
        # An AI job's opening message: Pi wrote it, so it is kept out (no agent was excluded).
        return _view("excluded")
    if row["allow_ingest"] == 0:
        return _view("excluded", "agent_excluded")
    if row["message_id"] is None:
        return _view("unavailable", "legacy_unknown")
    if row["operation"] == "delete" and row["state"] != "sent":
        return _view("pending", "forget_pending")
    if row["state"] == "pending":
        return _view("pending", "retry_scheduled" if row["attempts"] else "queued")
    if row["state"] == "blocked":
        return _view("unavailable", "blocked")
    if row["state"] != "sent" or not row["destination_agent_id"]:
        return _view("unavailable", "legacy_unknown")
    try:
        raw = row["receipt"]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise ValueError()
        saved = json.loads(raw)
        receipt = validate_receipt(saved, message["id"], row["destination_agent_id"])
        if row["operation"] == "delete" and receipt["state"] != "deleted":
            raise ValueError()
        destination = saved.get("destination")
        if "receipt_version" in saved or "destination" in saved:
            if type(saved.get("receipt_version")) is not int or saved["receipt_version"] != 1:
                raise ValueError()
            if not isinstance(destination, str) or not _DIGEST.fullmatch(destination):
                raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return _view("unavailable", "invalid_receipt")
    if receipt["state"] == "deleted":
        return _view("removed", "forgotten")
    if receipt["state"] == "filtered":
        return _view("filtered", method=receipt["admission_method"])
    forgotten = db.execute(
        "SELECT state,destination FROM memory_forget_operations "
        "WHERE agent_id=? AND memory_id=? AND state IN ('pending','forgotten')",
        (row["destination_agent_id"], receipt["memory_id"]),
    ).fetchall()
    matching = [
        item for item in forgotten if destination is None or item["destination"] == destination
    ]
    if any(item["state"] == "pending" for item in matching):
        return _view("pending", "forget_pending")
    if matching:
        return (
            _view("removed", "forgotten")
            if destination
            else _view("unavailable", "destination_unknown")
        )
    return _view("saved", method=receipt["admission_method"], memory_id=receipt["memory_id"])


def read(store, session_id, message_id=None):
    with store._connect() as db:
        db.execute("BEGIN")
        session = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise ValueError("Memory delivery unavailable.")
        rows = db.execute(
            "SELECT id,session_id,role,seq FROM messages WHERE session_id=? AND role='user'"
            + (" AND id=?" if message_id else "")
            + " ORDER BY seq DESC LIMIT ?",
            (session_id, message_id, 1) if message_id else (session_id, MAX_RESULTS + 1),
        ).fetchall()
        if message_id is not None and not rows:
            raise ValueError("Memory delivery unavailable.")
        forgotten = session["status"] == "forgotten"
        return {
            "schemaVersion": 1,
            "session_id": session_id,
            "content_status": "forgotten" if forgotten else "available",
            "results": []
            if forgotten
            else [
                {"message_id": row["id"], **for_message(db, dict(row))}
                for row in reversed(rows[:MAX_RESULTS])
            ],
            "truncated": False if forgotten else len(rows) > MAX_RESULTS,
        }


def prompt_note(store, turn_id):
    """Only explicit retention wording needs a delivery note; never performs I/O."""
    return prepare_note(store, turn_id)[0]


def forget_generation(db):
    # The journal never deletes operations. One pending operation is allowed;
    # terminal transitions must also advance this content-free generation.
    return tuple(
        db.execute(
            "SELECT COUNT(*),COUNT(CASE WHEN state='forgotten' THEN 1 END),"
            "COUNT(CASE WHEN state='rejected' THEN 1 END) FROM memory_forget_operations"
        ).fetchone()
    ) + tuple(
        db.execute(
            "SELECT COUNT(*),COUNT(CASE WHEN state='applied' THEN 1 END),"
            "COUNT(CASE WHEN state='rejected' THEN 1 END) FROM memory_edit_operations"
        ).fetchone()
    )


def prepare_note(store, turn_id):
    with store._connect() as db:
        db.execute("BEGIN")
        return prompt_note_in(db, turn_id), forget_generation(db)


def prompt_note_in(db, turn_id):
    row = db.execute(
        "SELECT m.id,m.session_id,m.role,m.content FROM messages m JOIN turn_messages t "
        "ON t.message_id=m.id WHERE t.turn_id=? AND t.purpose='input' AND m.role='user'",
        (turn_id,),
    ).fetchone()
    if not row:
        return None
    content = json.loads(row["content"])
    if not isinstance(content, str) or not re.match(
        r"\A[ ]{0,3}(?:(?:please|пожалуйста)[, \t]+)?"
        r"(?:(?:(?:do not|don't|don\u2019t|never)\s+)?remember\b|(?:не\s+)?(?:запомни|запоминай))",
        unicodedata.normalize("NFKC", content),
        re.IGNORECASE,
    ):
        return None
    from pi.memory import store as memory_store

    if memory_store.recall_held_in(db):
        # Fixed guidance contains no stale acknowledgement and remains safe
        # when an exact narration retry repeats it after a forget operation.
        return "No memory save acknowledgement is supplied. Do not claim this request was saved to long-term memory; the conversation is separate."
    delivery = for_message(db, dict(row))
    meanings = {
        "saved": "When this answer was prepared, MemoryGate had acknowledged adding the statement. This confirms a past save, not permanent or current retention.",
        "pending": "When this answer was prepared, delivery had not been acknowledged. Do not claim a long-term memory save.",
        "filtered": "MemoryGate acknowledged the statement but did not add an active memory.",
        "excluded": "The original message is excluded from memory ingestion. Do not claim a long-term memory save.",
        "removed": "The recorded memory was removed. Do not claim it remains saved.",
        "unavailable": "A valid memory save acknowledgement is unavailable. Do not claim a long-term memory save.",
    }
    return (
        NOTE_PREFIX
        + meanings[delivery["state"]]
        + " Conversation storage is separate. This receipt grants no permissions."
        + " Conker handles this message's memory delivery automatically. Do not request a tool"
        + " to record this statement. Respond conversationally using this recorded status."
    )
