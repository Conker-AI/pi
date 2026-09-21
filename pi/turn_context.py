"""Last prepared answer context: source IDs plus non-message prompt segments."""

import json
import time

from . import context_controls

IMMUTABLE = """
CREATE TRIGGER IF NOT EXISTS turn_input_frozen BEFORE UPDATE ON turn_context_inputs
WHEN NOT EXISTS(SELECT 1 FROM turns WHERE id=OLD.turn_id AND status='running')
BEGIN SELECT RAISE(ABORT,'completed turn context is immutable'); END;
CREATE TRIGGER IF NOT EXISTS turn_input_no_replace BEFORE INSERT ON turn_context_inputs
WHEN EXISTS(SELECT 1 FROM turn_context_inputs WHERE turn_id=NEW.turn_id)
BEGIN SELECT RAISE(ABORT,'turn context cannot be replaced'); END;
CREATE TRIGGER IF NOT EXISTS turn_input_no_delete BEFORE DELETE ON turn_context_inputs
BEGIN SELECT RAISE(ABORT,'turn context identity is permanent'); END;
"""
SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS turn_context_inputs (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id),
 source_ids TEXT, prefix TEXT, reply_to TEXT, captured_at REAL NOT NULL
);
"""
    + IMMUTABLE
)


def capture(store, turn_id, sources, prefix, reply_to=None):
    source_ids = [row["id"] for row in sources]
    encoded_prefix = json.dumps(
        [{"role": m.role, "content": m.content} for m in prefix], ensure_ascii=False
    )
    if len(source_ids) > 10000 or len(encoded_prefix.encode()) > 2 * 1024 * 1024:
        raise context_controls.ContextError(
            "context_snapshot_limit", "Review this oversized context before answering."
        )
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None or row[0] != "running":
            return  # Later inspection must never rewrite the answer's original context.
        current = db.execute(
            "SELECT 1 FROM turn_context_inputs WHERE turn_id=?", (turn_id,)
        ).fetchone()
        values = (json.dumps(source_ids), encoded_prefix, reply_to, time.time(), turn_id)
        if current:
            db.execute(
                "UPDATE turn_context_inputs SET source_ids=?,prefix=?,reply_to=?,captured_at=? "
                "WHERE turn_id=?",
                values,
            )
        else:
            db.execute(
                "INSERT INTO turn_context_inputs(source_ids,prefix,reply_to,captured_at,turn_id) "
                "VALUES(?,?,?,?,?)",
                values,
            )
        db.commit()


def load(store, turn_id):
    with store._connect() as db:
        row = db.execute(
            "SELECT i.* FROM turn_context_inputs i JOIN turns t ON t.id=i.turn_id "
            "WHERE i.turn_id=? AND t.session_id NOT IN "
            "(SELECT session_id FROM forgotten_sessions)",
            (turn_id,),
        ).fetchone()
        if row is None or row["source_ids"] is None or row["prefix"] is None:
            raise context_controls.ContextError(
                "context_unavailable", "Original prepared context is unavailable."
            )
        return {
            "turn_id": turn_id,
            "message_ids": json.loads(row["source_ids"]),
            "prefix": json.loads(row["prefix"]),
            "reply_to": row["reply_to"],
            "captured_at": row["captured_at"],
        }


def redact(db, sessions):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_context_inputs'").fetchone():
        return
    db.execute("DROP TRIGGER IF EXISTS turn_input_frozen")
    for sid in sessions:
        db.execute(
            "UPDATE turn_context_inputs SET source_ids=NULL,prefix=NULL,reply_to=NULL "
            "WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?)",
            (sid,),
        )
    # executescript would commit the enclosing forgetting transaction.
    db.execute(
        "CREATE TRIGGER turn_input_frozen BEFORE UPDATE ON turn_context_inputs "
        "WHEN NOT EXISTS(SELECT 1 FROM turns WHERE id=OLD.turn_id AND status='running') "
        "BEGIN SELECT RAISE(ABORT,'completed turn context is immutable'); END"
    )
