"""Durable delivery metadata; conversation text stays in the message table."""

import json
import time

from pi.memory import uses
from pi.memory.recall_text import (  # noqa: F401  (prompt_context: callers read it here)
    _METADATA,
    _contains_content,
    prompt_context,
    recalled,
)

MAX_CONTENT_CHARACTERS = 16000

_MESSAGE_SETTINGS = """COALESCE(
 (SELECT x.snapshot FROM turn_settings x JOIN turns t ON t.id=x.turn_id
  WHERE t.session_id=NEW.session_id AND t.status='running' ORDER BY t.started_at DESC LIMIT 1),
 (SELECT settings FROM session_settings WHERE session_id=NEW.session_id),
 '{"agentId":"companion","privacy":{"memoryDisabled":false,"harnessDisabled":false}}')"""

# The opening message of each AI job's run (`pi.schedule.agent_jobs`). Pi wrote it: the preamble,
# the run's date and time, why it started (a webhook's payload among them) and the job's
# instructions, which for a job Conker made the model wrote. Nobody typed it, so it never goes to
# MemoryGate; the job's chat still recalls memory, and what the owner types there is his own.
JOB_RUN_INPUTS = (
    "SELECT s.input_message_id FROM turn_submissions s JOIN scheduled_runs r "
    "ON r.id=s.request_id WHERE s.input_message_id IS NOT NULL"
)
# While Pi writes one: the chat's running turn is a run's and has no opening message yet
# (`pi.conversation.submissions.bind` links the turn to its request before writing it).
_JOB_RUN_OPENING = """SELECT 1 FROM turns t JOIN turn_submissions s ON s.turn_id=t.id
  JOIN scheduled_runs r ON r.id=s.request_id
  WHERE t.session_id=NEW.session_id AND t.status='running' AND s.input_message_id IS NULL"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS memory_outbox (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    operation TEXT NOT NULL CHECK(operation IN ('ingest','delete')),
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    receipt TEXT
);
CREATE INDEX IF NOT EXISTS memory_outbox_due ON memory_outbox(next_at) WHERE state='pending';
CREATE TABLE IF NOT EXISTS memory_contexts (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    status TEXT NOT NULL,
    package TEXT,
    recorded_at REAL NOT NULL
);
-- One number every clearing of saved recall advances (`invalidate_recall`: a memory forget or
-- correction, a forgotten chat, an erased import). Read before a recall is retrieved and compared
-- when it is saved, so a recall in flight across such a change never keeps what it removed.
CREATE TABLE IF NOT EXISTS recall_generation (
    id INTEGER PRIMARY KEY CHECK(id=1),
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO recall_generation VALUES (1, 0);
CREATE TABLE IF NOT EXISTS memory_forget_operations (
    request_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','forgotten','rejected')),
    cached_packages_cleared INTEGER NOT NULL DEFAULT 0,
    receipt TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_forget_one_pending
ON memory_forget_operations(state) WHERE state='pending';
-- Chats where text from outside Conker arrived as a user message (a webhook payload). MemoryGate
-- would read their messages as the owner's own words, so none is sent to it from then on, and
-- `pi.turns.access` counts the whole chat as holding outside content. A fork keeps the mark.
CREATE TABLE IF NOT EXISTS outside_sessions (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    reason TEXT NOT NULL CHECK(reason IN ('webhook')),
    created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS outside_sessions_inherit AFTER INSERT ON sessions
WHEN NEW.parent_id IS NOT NULL
BEGIN
    INSERT OR IGNORE INTO outside_sessions
    SELECT NEW.id, reason, created_at FROM outside_sessions WHERE session_id=NEW.parent_id;
END;
DROP TRIGGER IF EXISTS messages_queue_memory;
CREATE TRIGGER messages_queue_memory AFTER INSERT ON messages
BEGIN
    INSERT INTO message_privacy VALUES (NEW.id,
      json_extract({_MESSAGE_SETTINGS},'$.privacy.memoryDisabled'),
      json_extract({_MESSAGE_SETTINGS},'$.privacy.harnessDisabled'),
      NOT json_extract({_MESSAGE_SETTINGS},'$.privacy.memoryDisabled')
      AND json_extract({_MESSAGE_SETTINGS},'$.agentId')='companion'
      AND NOT EXISTS(SELECT 1 FROM outside_sessions WHERE session_id=NEW.session_id)
      AND NOT EXISTS({_JOB_RUN_OPENING}));
    INSERT INTO memory_outbox(message_id, operation)
    SELECT NEW.id,'ingest' FROM message_privacy
    WHERE message_id=NEW.id AND allow_ingest=1 AND NEW.role='user';
END;
-- Existing messages predate privacy controls; retain their original eligibility.
INSERT INTO message_privacy
SELECT id,0,0,1 FROM messages WHERE NOT EXISTS(SELECT 1 FROM message_privacy p WHERE p.message_id=messages.id);
-- Upgrade existing transcripts too, including tombstones from before this bridge.
INSERT OR IGNORE INTO memory_outbox(message_id, operation)
SELECT m.id, CASE WHEN f.session_id IS NULL THEN 'ingest' ELSE 'delete' END
FROM messages m LEFT JOIN forgotten_sessions f ON f.session_id=m.session_id
WHERE m.role='user' AND (f.session_id IS NOT NULL OR EXISTS(
 SELECT 1 FROM message_privacy p WHERE p.message_id=m.id AND p.allow_ingest=1));
"""


def _keep_out_old_job_runs(db):
    """Older Pi queued job runs' opening messages like the owner's: withdraw those not yet sent.
    One already sent stays as its receipt records it (recall never counts it as his words)."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"turn_submissions", "scheduled_runs"} <= tables:
        return  # A new database: nothing was queued before.
    db.execute(
        "DELETE FROM memory_outbox WHERE operation='ingest' AND state='pending' "
        f"AND delivery_started=0 AND message_id IN ({JOB_RUN_INPUTS})"
    )


def job_run_input(db, message_id) -> bool:
    """Whether this message opened an AI job's run."""
    return bool(db.execute(f"SELECT 1 WHERE ? IN ({JOB_RUN_INPUTS})", (message_id,)).fetchone())


def migrate(db):
    forget_columns = {row[1] for row in db.execute("PRAGMA table_info(memory_forget_operations)")}
    if "destination" not in forget_columns:
        # Never guess the destination of an already-attempted old request.
        db.execute(
            "ALTER TABLE memory_forget_operations ADD COLUMN destination TEXT NOT NULL DEFAULT ''"
        )
    columns = {row[1] for row in db.execute("PRAGMA table_info(memory_outbox)")}
    if "destination_agent_id" not in columns:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE memory_outbox ADD COLUMN destination_agent_id TEXT")
        db.execute(
            "ALTER TABLE memory_outbox ADD COLUMN delivery_started INTEGER NOT NULL DEFAULT 0"
        )
        # Older failed requests may have committed remotely before losing the ACK.
        # Neither the current config nor an empty receipt proves their destination.
        db.execute(
            "UPDATE memory_outbox SET delivery_started=1 "
            "WHERE attempts>0 OR state='sent' OR operation='delete'"
        )
        db.commit()
    _keep_out_old_job_runs(db)
    db.commit()


def pin_destination(store, message_id, agent_id):
    from pi.conversation import settings as session_settings
    from pi.core.errors import ServiceError

    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        eligibility = db.execute(
            "SELECT allow_ingest FROM message_privacy WHERE message_id=?", (message_id,)
        ).fetchone()
        operation = db.execute(
            "SELECT operation FROM memory_outbox WHERE message_id=?", (message_id,)
        ).fetchone()
        if operation and operation[0] == "ingest" and (not eligibility or not eligibility[0]):
            raise ValueError("This message is excluded from memory ingestion.")
        if operation and operation[0] == "ingest":
            origin = db.execute(
                "SELECT session_id FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            try:
                floor = session_settings.inherited_privacy(db, origin[0]) if origin else None
            except ServiceError:
                raise ValueError(
                    "Inherited source privacy is unavailable; memory ingestion is excluded."
                ) from None
            if floor is None or floor["memoryDisabled"]:
                raise ValueError(
                    "Inherited messages exclude this conversation from memory ingestion."
                )
        row = db.execute(
            "SELECT destination_agent_id,delivery_started FROM memory_outbox WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if row[0] is None and row[1]:
            raise ValueError(
                "Original MemoryGate namespace unknown. Stop Pi and use "
                "python -m pi.memory.recovery bind-origin with the original agent ID."
            )
        destination = row[0] if row[0] is not None else agent_id
        db.execute(
            "UPDATE memory_outbox SET destination_agent_id=?,delivery_started=1 WHERE message_id=?",
            (destination, message_id),
        )
        db.commit()
    return destination


def pending_deletions(store):
    with store._connect() as db:
        return pending_deletions_in(db)


def pending_deletions_in(db):
    return (
        db.execute(
            "SELECT COUNT(*) FROM memory_outbox WHERE operation='delete' AND state!='sent'"
        ).fetchone()[0]
        + db.execute(
            "SELECT COUNT(*) FROM memory_forget_operations WHERE state='pending'"
        ).fetchone()[0]
    )


def recall_held_in(db):
    return (
        pending_deletions_in(db)
        + db.execute(
            "SELECT COUNT(*) FROM memory_edit_operations WHERE state IN ('applying','unknown')"
        ).fetchone()[0]
    )


def recall_held(store):
    with store._connect() as db:
        return recall_held_in(db)


def generation_in(db) -> int:
    """The recall generation (see SCHEMA). Pi creates it at start, before any recall."""
    return db.execute("SELECT value FROM recall_generation WHERE id=1").fetchone()[0]


def generation(store) -> int:
    with store._connect() as db:
        return generation_in(db)


def invalidate_recall(db):
    """Historical briefings lack IDs; invalidate derived recall, never conversation text."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='recall_generation'").fetchone():
        # Absent only for offline forgetting before the Store first opens: no recall is in flight.
        db.execute("UPDATE recall_generation SET value=value+1 WHERE id=1")
    changed = db.execute(
        "UPDATE memory_contexts SET package=NULL,status='redacted' WHERE package IS NOT NULL"
    ).rowcount
    redact_recall(db)
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recall_results'"
    ).fetchone():
        db.execute("UPDATE recall_results SET package=NULL WHERE package IS NOT NULL")
    return changed


def redact_recall(db):
    """Invalidate exact historical inputs, including retries without a context package.

    The recall and delivery envelopes have stable system-message prefixes. They
    cover unlabelled briefing content and recorded acknowledgements. Source messages
    are not searched for matching words, and no-memory snapshots remain usable.
    """
    from pi.memory import delivery as memory_delivery

    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_context_inputs'").fetchone():
        return
    trigger = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='turn_input_frozen'"
    ).fetchone()
    if trigger:
        db.execute("DROP TRIGGER turn_input_frozen")
    try:
        db.execute(
            "UPDATE turn_context_inputs SET prefix=NULL WHERE prefix IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM json_each(turn_context_inputs.prefix) message "
            "WHERE json_extract(message.value,'$.role')='system' "
            "AND (substr(json_extract(message.value,'$.content'),1,?)=? "
            "OR substr(json_extract(message.value,'$.content'),1,?)=?))",
            (
                len(memory_delivery.RECALL_HEADER),
                memory_delivery.RECALL_HEADER,
                len(memory_delivery.NOTE_PREFIX),
                memory_delivery.NOTE_PREFIX,
            ),
        )
    finally:
        if trigger:
            db.execute(trigger[0])


def save_context(store, turn_id, status, package=None, generation=None):
    """`generation`: the recall generation read before retrieving `package`. A change that cleared
    saved recall since then may have removed what it holds, so it is not kept."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if package is not None and (
            recall_held_in(db) or (generation is not None and generation != generation_in(db))
        ):
            status, package = "unavailable", None
        db.execute(
            "INSERT INTO memory_contexts VALUES (?,?,?,?)",
            (
                turn_id,
                status,
                json.dumps(package, ensure_ascii=False) if package else None,
                time.time(),
            ),
        )
        if package:
            turn = db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)).fetchone()
            if turn:
                uses.record(db, turn_id, turn["session_id"], package, "context")
        db.commit()


def context(store, turn_id):
    with store._connect() as db:
        return _context_in(db, turn_id, recall_held_in(db))[0]


def _context_in(db, turn_id, held):
    """(A turn's saved recall, the turn's chat): one read. `held` is recall_held_in's answer."""
    row = db.execute(
        "SELECT c.turn_id,c.status,c.package,c.recorded_at,t.session_id FROM memory_contexts c "
        "LEFT JOIN turns t ON t.id=c.turn_id WHERE c.turn_id=?",
        (turn_id,),
    ).fetchone()
    if not row:
        return {"status": "not_recorded", "package": None}, None
    saved = {key: row[key] for key in ("turn_id", "status", "package", "recorded_at")}
    if held and row["package"]:
        return {**saved, "status": "unavailable", "package": None}, row["session_id"]
    package = json.loads(row["package"]) if row["package"] else None
    return {**saved, "package": package}, row["session_id"]


# MemoryGate's `source_type` of a memory holding the owner's own words: a statement he typed or
# approved word for word ("user"), or a memory made from one of his chat messages
# ("owner_statement", which cites them). Conker's own statements ("assistant"), listener and
# imported content ("automatic_listener"), documents, skills and the rest are someone else's.
OWNER_SOURCES = ("user", "owner_statement")


def others_wrote(db, package) -> bool:
    """Whether recalled evidence holds any text the owner did not write himself (`pi.turns.access`).

    Fails closed. Entities, episodes, evidence, a briefing and any field Pi does not know count as
    someone else's (MemoryGate derives them). A memory is his only with an owner source; one made
    from his messages only when each message it cites is one Pi holds as his, typed in a chat that
    sends to memory (never an AI job's opening message, even one an older Pi sent).
    """
    if not recalled(package):
        return False
    if any(
        _contains_content(value)
        for key, value in package.items()
        if key not in ("memories", *_METADATA)
    ):
        return True
    memories = package.get("memories")
    if not isinstance(memories, list):
        return True
    cited = set()
    for item in memories:
        source = item.get("source_type") if isinstance(item, dict) else None
        if source not in OWNER_SOURCES:
            return True
        if source == "user":
            continue
        citations = item.get("citations")
        ids = (
            [entry.get("message_id") if isinstance(entry, dict) else None for entry in citations]
            if isinstance(citations, list)
            else []
        )
        if not ids or not all(isinstance(value, str) and value for value in ids):
            return True
        cited.update(ids)
    if not cited:
        return False
    marks = ",".join("?" for _ in cited)
    his = db.execute(
        "SELECT COUNT(*) FROM messages m JOIN message_privacy p ON p.message_id=m.id "
        f"WHERE m.id IN ({marks}) AND m.role='user' AND p.allow_ingest=1 "
        f"AND m.id NOT IN ({JOB_RUN_INPUTS})",
        sorted(cited),
    ).fetchone()[0]
    return his != len(cited)


def delivery(store, session_id=None):
    from pi.memory import delivery as memory_delivery

    with store._connect() as db:
        rows = db.execute(
            "SELECT o.operation,o.state,o.error,o.receipt,o.message_id,o.destination_agent_id FROM memory_outbox o"
            " JOIN messages m ON m.id=o.message_id"
            + (" WHERE m.session_id=?" if session_id else ""),
            (session_id,) if session_id else (),
        ).fetchall()
    pending = sum(row["state"] == "pending" and row["operation"] == "ingest" for row in rows)
    blocked = sum(row["state"] == "blocked" for row in rows)
    deleting = pending_deletions(store)
    receipts = []
    for row in rows:
        raw = row["receipt"]
        try:
            if (
                row["state"] != "sent"
                or not isinstance(raw, str)
                or len(raw.encode("utf-8")) > memory_delivery.MAX_RECEIPT_BYTES
            ):
                continue
            receipts.append(
                memory_delivery.validate_receipt(
                    json.loads(raw), row["message_id"], row["destination_agent_id"]
                )
            )
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            # Historical acknowledgement metadata cannot make the transcript
            # unavailable or establish a save from a malformed receipt.
            continue
    admission = {
        state: sum(receipt.get("state") == state for receipt in receipts)
        for state in ("admitted", "filtered", "deleted")
    }
    delivery_error = next((row["error"] for row in rows if row["error"]), None)
    return {
        "pending": pending,
        "blocked": blocked,
        "deleting": deleting,
        "admission": admission,
        "delivery_error": delivery_error,
    }


def _status(part, retrieval, configured):
    notices = []
    if part["blocked"]:
        notices.append(
            "Conversation saved. Long-term memory delivery is blocked; "
            "see delivery_error and repair it before explicitly retrying."
        )
    if retrieval and retrieval["status"] == "disabled":
        notices.append(
            "Memory is excluded for this turn by privacy or an unmapped specialist namespace. Prior allowed deliveries remain separate."
        )
    elif not configured:
        notices.append(
            "Conversation saved. Long-term memory is not configured; delivery remains pending."
        )
    elif part["pending"]:
        notices.append(
            "Conversation saved. Long-term memory is pending; Pi will retry automatically."
        )
    if part["deleting"]:
        notices.append(
            "Forgetting is pending in MemoryGate. "
            "Memory retrieval is paused until deletion is acknowledged."
        )
    if retrieval and retrieval["status"] in {"unavailable", "degraded", "redacted", "not_recorded"}:
        notices.append(
            "Memory context is incomplete or unavailable for this turn; "
            "the conversation continues with that gap."
        )
    package = (retrieval or {}).get("package")
    metadata = package.get("retrieval") if isinstance(package, dict) else None
    budget = metadata.get("pi_context_budget") if isinstance(metadata, dict) else None
    if (
        isinstance(budget, dict)
        and budget.get("status") in {"partial", "empty"}
        and all(
            isinstance(budget.get(key), dict)
            and all(type(value) is int and value >= 0 for value in budget[key].values())
            for key in ("received", "retained", "omitted")
        )
    ):
        if budget["status"] == "empty":
            notices.append("No retrieved evidence fit within this turn's memory size limit.")
        elif sum(budget["omitted"].values()):
            notices.append(
                f"Memory context kept {sum(budget['retained'].values())} of "
                f"{sum(budget['received'].values())} retrieved records within its size limit; "
                "omitted records were not supplied to the model."
            )
        else:
            notices.append(
                "Memory context omitted optional package fields to stay within its size limit."
            )
    return {
        "configured": configured,
        "pending_ingestion": part["pending"],
        "blocked_delivery": part["blocked"],
        "pending_deletion": part["deleting"],
        "admission": part["admission"],
        "delivery_error": part["delivery_error"],
        "retrieval": retrieval,
        "notices": notices,
    }


def _shown(store, turn_ids, agents=()):
    """Each turn's saved recall as the owner may see it now, on one connection and snapshot:
    without the records of another chat in the Trash or forgotten (pi.conversation.hidden_records,
    the rule recall itself uses). `agents` are the reading agents: they name the records of a
    message sent before Pi recorded where it went."""
    from pi.conversation import hidden_records

    reading, shown = hidden_records.Reading(), {}
    with store._connect() as db:
        db.execute("BEGIN")
        held = recall_held_in(db)
        for turn_id in turn_ids:
            saved, own = _context_in(db, turn_id, held)
            package = saved["package"]
            kept = hidden_records.filter_package(store, package, agents, own, reading, db)
            shown[turn_id] = saved if kept is package else {**saved, "package": kept}
    return shown


def status(store, session_id=None, turn_id=None, *, configured=False, agents=()):
    retrieval = _shown(store, [turn_id], agents)[turn_id] if turn_id else None
    return _status(delivery(store, session_id), retrieval, configured)


def statuses(store, session_id, turn_ids, *, configured=False, agents=()):
    part, shown = delivery(store, session_id), _shown(store, turn_ids, agents)
    return {
        "session": _status(part, None, configured),
        "turns": {tid: _status(part, shown[tid], configured) for tid in turn_ids},
    }


def redact(db, session_ids):
    from pi.memory import edits as memory_edits

    memory_edits.redact_sources(db, session_ids)
    # Context packages can quote a forgotten message in any session. Clearing
    # all cached packages avoids relying on incomplete third-party lineage.
    invalidate_recall(db)
    for sid in session_ids:
        db.execute(
            "UPDATE memory_outbox SET operation='delete',state='pending',attempts=0,"
            "next_at=0,error='',receipt=NULL WHERE message_id IN"
            " (SELECT id FROM messages WHERE session_id=? AND role='user')",
            (sid,),
        )
