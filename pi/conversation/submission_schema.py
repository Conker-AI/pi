"""Tables of durable submission receipts and their turn/message associations.

Behaviour lives in pi.conversation.submissions.
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_submissions (
    request_id TEXT PRIMARY KEY,
    requested_session_id TEXT NOT NULL REFERENCES sessions(id),
    effective_session_id TEXT REFERENCES sessions(id),
    turn_id TEXT UNIQUE REFERENCES turns(id),
    task_id TEXT REFERENCES tasks(id), task_expected_revision INTEGER,
    input_message_id TEXT REFERENCES messages(id),
    state TEXT NOT NULL CHECK(state IN
        ('preparing','bound','preparation_failed','preparation_interrupted','forgotten')),
    payload_hash TEXT, pending_text TEXT,
    history_seq INTEGER NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    failure_code TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS submissions_one_preparing
ON turn_submissions(requested_session_id) WHERE state='preparing';
CREATE INDEX IF NOT EXISTS submissions_effective ON turn_submissions(effective_session_id);
CREATE TRIGGER IF NOT EXISTS submissions_no_replace BEFORE INSERT ON turn_submissions
WHEN EXISTS(SELECT 1 FROM turn_submissions WHERE request_id=NEW.request_id)
BEGIN SELECT RAISE(ABORT,'submission identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS submissions_no_delete BEFORE DELETE ON turn_submissions
BEGIN SELECT RAISE(ABORT,'submission identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS submissions_fixed_origin BEFORE UPDATE ON turn_submissions
WHEN NEW.request_id IS NOT OLD.request_id
  OR NEW.requested_session_id IS NOT OLD.requested_session_id
  OR NEW.task_id IS NOT OLD.task_id OR NEW.task_expected_revision IS NOT OLD.task_expected_revision
  OR NEW.history_seq IS NOT OLD.history_seq OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.turn_id IS NOT NULL AND NEW.turn_id IS NOT OLD.turn_id)
  OR (OLD.effective_session_id IS NOT NULL AND
      NEW.effective_session_id IS NOT OLD.effective_session_id)
  OR (OLD.input_message_id IS NOT NULL AND NEW.input_message_id IS NOT OLD.input_message_id)
  OR (NEW.payload_hash IS NOT OLD.payload_hash AND
      NOT (NEW.payload_hash IS NULL AND NEW.state='forgotten'))
  OR (NEW.pending_text IS NOT OLD.pending_text AND NEW.pending_text IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'submission identity and input are fixed'); END;
CREATE TABLE IF NOT EXISTS turn_messages (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    turn_id TEXT NOT NULL REFERENCES turns(id),
    purpose TEXT NOT NULL CHECK(purpose IN ('input','intermediate','tool_result','final')),
    action_id TEXT REFERENCES tool_actions(id)
);
CREATE INDEX IF NOT EXISTS messages_by_turn ON turn_messages(turn_id);
CREATE UNIQUE INDEX IF NOT EXISTS one_turn_input ON turn_messages(turn_id) WHERE purpose='input';
CREATE UNIQUE INDEX IF NOT EXISTS one_turn_final ON turn_messages(turn_id) WHERE purpose='final';
CREATE UNIQUE INDEX IF NOT EXISTS one_action_observation ON turn_messages(action_id)
WHERE action_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS turn_messages_valid BEFORE INSERT ON turn_messages
WHEN NOT EXISTS (
    SELECT 1 FROM messages m JOIN turns t ON t.session_id=m.session_id
    WHERE m.id=NEW.message_id AND t.id=NEW.turn_id AND t.status='running'
    AND ((NEW.purpose='input' AND m.role='user')
      OR (NEW.purpose IN ('intermediate','final') AND m.role='assistant')
      OR (NEW.purpose='tool_result' AND m.role='tool'))
    AND (NEW.action_id IS NULL OR (NEW.purpose='tool_result' AND EXISTS
        (SELECT 1 FROM tool_actions a WHERE a.id=NEW.action_id AND a.turn_id=t.id)))
)
BEGIN SELECT RAISE(ABORT,'invalid turn message association'); END;
CREATE TRIGGER IF NOT EXISTS turn_messages_no_update BEFORE UPDATE ON turn_messages
BEGIN SELECT RAISE(ABORT,'turn message associations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS turn_messages_no_delete BEFORE DELETE ON turn_messages
BEGIN SELECT RAISE(ABORT,'turn message associations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS turn_messages_no_replace BEFORE INSERT ON turn_messages
WHEN EXISTS (SELECT 1 FROM turn_messages WHERE message_id=NEW.message_id)
BEGIN SELECT RAISE(ABORT,'turn message associations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS turn_messages_unique_no_replace BEFORE INSERT ON turn_messages
WHEN EXISTS (SELECT 1 FROM turn_messages WHERE
    (NEW.purpose IN ('input','final') AND turn_id=NEW.turn_id AND purpose=NEW.purpose)
    OR (NEW.action_id IS NOT NULL AND action_id=NEW.action_id))
BEGIN SELECT RAISE(ABORT,'turn message associations are immutable'); END;
"""
