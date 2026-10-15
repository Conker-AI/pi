"""Content-free change tokens for observing work started by another client.

Counters change in the transaction that changes history, not when a browser
reads them. Read this token before loading history: a later token must never be
used to acknowledge an earlier, possibly nontransactional history projection.
"""

MAX_SAFE_REVISION = (1 << 53) - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_observations (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0)
);
INSERT OR IGNORE INTO session_observations SELECT id,0 FROM sessions;
CREATE INDEX IF NOT EXISTS observation_running_turns ON turns(session_id) WHERE status='running';
CREATE INDEX IF NOT EXISTS observation_active_thinking ON background_thinking(session_id)
 WHERE status IN ('queued','running');
CREATE TRIGGER IF NOT EXISTS session_observation_session_insert AFTER INSERT ON sessions
BEGIN
 INSERT INTO session_observations VALUES(NEW.id,1)
 ON CONFLICT(session_id) DO UPDATE SET revision=revision+1;
END;
"""


def _trigger(table, operation, identities):
    # Identifiers/expressions below are fixed schema definitions, never request input.
    return f"""
CREATE TRIGGER IF NOT EXISTS session_observation_{table}_{operation.lower()}
AFTER {operation} ON {table}
BEGIN
 UPDATE session_observations SET revision=revision+1
 WHERE session_id IN ({identities});
END;
"""


SCHEMA += _trigger("sessions", "UPDATE", "SELECT OLD.id UNION SELECT NEW.id")
for _table in ("messages", "background_thinking", "forgotten_sessions", "response_families"):
    for _operation in ("INSERT", "UPDATE", "DELETE"):
        _rows = (
            ("NEW",)
            if _operation == "INSERT"
            else ("OLD",)
            if _operation == "DELETE"
            else ("OLD", "NEW")
        )
        SCHEMA += _trigger(
            _table, _operation, " UNION ".join(f"SELECT {row}.session_id" for row in _rows)
        )
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _rows = (
        ("NEW",)
        if _operation == "INSERT"
        else ("OLD",)
        if _operation == "DELETE"
        else ("OLD", "NEW")
    )
    SCHEMA += _trigger(
        "turns",
        _operation,
        " UNION ".join(f"SELECT {row}.session_id" for row in _rows)
        + " UNION SELECT requested_session_id FROM turn_submissions WHERE turn_id IN ("
        + ",".join(f"{row}.id" for row in _rows)
        + ")",
    )
    SCHEMA += _trigger(
        "turn_submissions",
        _operation,
        " UNION ".join(
            f"SELECT {row}.{column}"
            for row in _rows
            for column in ("requested_session_id", "effective_session_id")
        ),
    )
    SCHEMA += _trigger(
        "tool_actions",
        _operation,
        "SELECT session_id FROM turns WHERE id IN ("
        + ",".join(f"{row}.turn_id" for row in _rows)
        + ")",
    )
    SCHEMA += _trigger(
        "response_versions",
        _operation,
        "SELECT session_id FROM messages WHERE id IN ("
        + ",".join(f"{row}.message_id" for row in _rows)
        + ")",
    )


def initialize(db):
    # Run after the observed tables exist; backfill never resets existing counters.
    db.executescript(SCHEMA)


def read(store, session_id):
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute(
            "SELECT s.status,o.revision,EXISTS(SELECT 1 FROM forgotten_sessions f "
            "WHERE f.session_id=s.id) AS forgotten FROM sessions s "
            "JOIN session_observations o ON o.session_id=s.id WHERE s.id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Conversation not found.")
        revision = row["revision"]
        # Do not round, wrap or saturate a counter into a misleading unchanged token.
        if type(revision) is not int or not 0 <= revision <= MAX_SAFE_REVISION:
            raise OverflowError("Conversation observation unavailable.")
        forgotten = bool(row["forgotten"] or row["status"] == "forgotten")
        active = (
            False
            if forgotten
            else bool(
                db.execute(
                    "SELECT EXISTS(SELECT 1 FROM turns WHERE session_id=? AND status='running') "
                    "OR EXISTS(SELECT 1 FROM turns t JOIN turn_submissions s ON s.turn_id=t.id "
                    "WHERE s.requested_session_id=? AND t.status='running') "
                    "OR EXISTS(SELECT 1 FROM turn_submissions "
                    "WHERE (requested_session_id=? OR effective_session_id=?) AND state='preparing') "
                    "OR EXISTS(SELECT 1 FROM background_thinking "
                    "WHERE session_id=? AND status IN ('queued','running'))",
                    (session_id, session_id, session_id, session_id, session_id),
                ).fetchone()[0]
            )
        )
        return {
            "schemaVersion": 1,
            "session_id": session_id,
            "revision": revision,
            "active": active,
            "content_status": "forgotten" if forgotten else "available",
        }
