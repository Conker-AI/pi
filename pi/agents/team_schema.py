"""Tables and immutability triggers of team runs, steps and model calls (a pure move out of
team_execution.py to keep that file under the size limit)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_runs (
 id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL,
 team_id TEXT NOT NULL REFERENCES collaboration_records(id),
 source_session_id TEXT NOT NULL REFERENCES sessions(id),
 root_session_id TEXT NOT NULL REFERENCES sessions(id), input_message_id TEXT NOT NULL,
 snapshot TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'ready', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_steps (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES team_runs(id),
 request_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL,
 role_id TEXT NOT NULL, handoff_id TEXT,
 session_id TEXT UNIQUE NOT NULL REFERENCES sessions(id),
 task_id TEXT NOT NULL, snapshot TEXT NOT NULL, source_ids TEXT NOT NULL,
 previous_message_id TEXT, with_citations INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'running', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_model_calls (
 id TEXT PRIMARY KEY, step_id TEXT NOT NULL REFERENCES team_steps(id),
 provider TEXT NOT NULL, requested_model TEXT NOT NULL, actual_model TEXT,
 state TEXT NOT NULL DEFAULT 'running', input_tokens INTEGER, output_tokens INTEGER,
 cost_usd REAL, latency_ms INTEGER
);
CREATE TRIGGER IF NOT EXISTS team_submission_identity BEFORE INSERT ON turn_submissions
WHEN EXISTS (SELECT 1 FROM team_steps WHERE session_id=NEW.requested_session_id
 AND request_id!=NEW.request_id)
BEGIN SELECT RAISE(ABORT,'team sessions accept only their reserved step'); END;
CREATE TRIGGER IF NOT EXISTS team_context_fixed BEFORE UPDATE ON context_policies
WHEN EXISTS(SELECT 1 FROM team_steps WHERE session_id=NEW.session_id)
BEGIN SELECT RAISE(ABORT,'team step context is fixed'); END;
CREATE TRIGGER IF NOT EXISTS team_context_no_replace BEFORE INSERT ON context_policies
WHEN EXISTS(SELECT 1 FROM team_steps WHERE session_id=NEW.session_id)
BEGIN SELECT RAISE(ABORT,'team step context is fixed'); END;
"""
for _table, _columns in {
    "team_runs": [
        "id",
        "request_id",
        "payload_hash",
        "team_id",
        "source_session_id",
        "root_session_id",
        "input_message_id",
        "snapshot",
    ],
    "team_steps": [
        "id",
        "run_id",
        "request_id",
        "payload_hash",
        "role_id",
        "handoff_id",
        "session_id",
        "task_id",
        "snapshot",
        "source_ids",
        "previous_message_id",
        "with_citations",
    ],
}.items():
    _changed = " OR ".join(f"NEW.{key} IS NOT OLD.{key}" for key in _columns)
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_fixed BEFORE UPDATE ON {_table}
WHEN {_changed} BEGIN SELECT RAISE(ABORT,'team execution input is immutable'); END;
CREATE TRIGGER IF NOT EXISTS {_table}_no_delete BEFORE DELETE ON {_table}
BEGIN SELECT RAISE(ABORT,'team execution receipts are retained'); END;
CREATE TRIGGER IF NOT EXISTS {_table}_no_replace BEFORE INSERT ON {_table}
WHEN EXISTS(SELECT 1 FROM {_table} WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'team execution identity is permanent'); END;
"""
