"""Attachment tables and the triggers that keep removal receipts and message bindings immutable."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 name TEXT NOT NULL, media_type TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
 last_modified REAL NOT NULL, created_at REAL NOT NULL,
 content BLOB, sha256 TEXT, extracted_text TEXT,
 processing TEXT NOT NULL, reason TEXT NOT NULL, privacy TEXT,
 purged INTEGER NOT NULL DEFAULT 0 CHECK(purged IN (0,1)),
 CHECK((purged=0 AND content IS NOT NULL AND sha256 IS NOT NULL AND privacy IS NOT NULL)
    OR (purged=1 AND content IS NULL AND sha256 IS NULL AND extracted_text IS NULL
        AND privacy IS NULL AND name='' AND media_type=''))
);
CREATE INDEX IF NOT EXISTS attachments_session ON attachments(session_id);
CREATE TABLE IF NOT EXISTS attachment_reservations (
 request_id TEXT NOT NULL REFERENCES turn_submissions(request_id),
 attachment_id TEXT NOT NULL REFERENCES attachments(id),
 PRIMARY KEY(request_id,attachment_id)
);
CREATE INDEX IF NOT EXISTS attachments_reserved ON attachment_reservations(attachment_id);
CREATE TRIGGER IF NOT EXISTS attachment_reservations_no_update
BEFORE UPDATE ON attachment_reservations
BEGIN SELECT RAISE(ABORT,'attachment reservations have fixed identities'); END;
CREATE TRIGGER IF NOT EXISTS attachment_reservations_no_replace
BEFORE INSERT ON attachment_reservations
WHEN EXISTS(SELECT 1 FROM attachment_reservations
 WHERE request_id=NEW.request_id AND attachment_id=NEW.attachment_id)
BEGIN SELECT RAISE(ABORT,'attachment reservations have fixed identities'); END;
CREATE TRIGGER IF NOT EXISTS attachment_reservations_valid BEFORE INSERT ON attachment_reservations
WHEN NOT EXISTS(SELECT 1 FROM attachments a JOIN turn_submissions s
 ON s.requested_session_id=a.session_id WHERE a.id=NEW.attachment_id
 AND a.purged=0 AND s.request_id=NEW.request_id AND s.state='preparing')
BEGIN SELECT RAISE(ABORT,'reservation requires an available originating attachment'); END;
CREATE TABLE IF NOT EXISTS attachment_removals (
 id TEXT PRIMARY KEY, attachment_id TEXT NOT NULL UNIQUE REFERENCES attachments(id),
 removed_at REAL NOT NULL, reason TEXT NOT NULL CHECK(reason='owner-removed')
);
CREATE TABLE IF NOT EXISTS message_attachment_sets (
 message_id TEXT PRIMARY KEY REFERENCES messages(id), attachment_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_attachments (
 message_id TEXT NOT NULL REFERENCES messages(id), position INTEGER NOT NULL,
 attachment_id TEXT NOT NULL REFERENCES attachments(id),
 PRIMARY KEY(message_id,position), UNIQUE(message_id,attachment_id)
);
CREATE TRIGGER IF NOT EXISTS attachments_no_replace BEFORE INSERT ON attachments
WHEN EXISTS(SELECT 1 FROM attachments WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'attachment identities are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attachments_no_update BEFORE UPDATE ON attachments
WHEN NOT (NEW.id=OLD.id AND NEW.session_id=OLD.session_id AND NEW.size=OLD.size
 AND NEW.created_at=OLD.created_at AND NEW.last_modified=OLD.last_modified
 AND OLD.purged=0 AND NEW.purged=1 AND NEW.content IS NULL AND NEW.sha256 IS NULL
 AND NEW.extracted_text IS NULL AND NEW.privacy IS NULL AND NEW.name='' AND NEW.media_type=''
 AND NEW.processing='unavailable' AND (NEW.reason='forgotten' OR (NEW.reason='removed'
  AND EXISTS(SELECT 1 FROM attachment_removals WHERE attachment_id=OLD.id))))
BEGIN SELECT RAISE(ABORT,'attachment bytes and provenance are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attachments_no_delete BEFORE DELETE ON attachments
BEGIN SELECT RAISE(ABORT,'attachment identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS attachments_valid_source BEFORE INSERT ON attachments
WHEN NOT EXISTS(SELECT 1 FROM sessions WHERE id=NEW.session_id AND status='open')
 OR EXISTS(SELECT 1 FROM forgotten_sessions WHERE session_id=NEW.session_id)
BEGIN SELECT RAISE(ABORT,'attachment requires an active session'); END;
CREATE TRIGGER IF NOT EXISTS message_attachments_valid BEFORE INSERT ON message_attachments
WHEN NOT EXISTS(SELECT 1 FROM messages m JOIN attachments a ON a.session_id=m.session_id
 WHERE m.id=NEW.message_id AND a.id=NEW.attachment_id AND a.purged=0 AND m.role='user'
 AND NOT EXISTS(SELECT 1 FROM forgotten_sessions f WHERE f.session_id=m.session_id))
BEGIN SELECT RAISE(ABORT,'attachment and user message must share an available session'); END;
CREATE TRIGGER IF NOT EXISTS message_attachments_exact_set BEFORE INSERT ON message_attachments
WHEN NOT EXISTS(SELECT 1 FROM message_attachment_sets WHERE message_id=NEW.message_id
 AND json_extract(attachment_ids,'$[' || NEW.position || ']')=NEW.attachment_id)
BEGIN SELECT RAISE(ABORT,'attachment must match the sealed message set'); END;
CREATE TRIGGER IF NOT EXISTS message_attachment_sets_no_replace
BEFORE INSERT ON message_attachment_sets
WHEN EXISTS(SELECT 1 FROM message_attachment_sets WHERE message_id=NEW.message_id)
BEGIN SELECT RAISE(ABORT,'message attachment sets are immutable'); END;
"""
for _operation in ("UPDATE", "DELETE"):
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS attachment_removals_no_{_operation.lower()}
BEFORE {_operation} ON attachment_removals
BEGIN SELECT RAISE(ABORT,'attachment removal receipts are immutable'); END;
"""
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS message_attachment_sets_no_{_operation.lower()}
BEFORE {_operation} ON message_attachment_sets
BEGIN SELECT RAISE(ABORT,'message attachment sets are immutable'); END;
"""
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS message_attachments_no_{_operation.lower()}
BEFORE {_operation} ON message_attachments
BEGIN SELECT RAISE(ABORT,'message attachment bindings are immutable'); END;
"""
SCHEMA += """
CREATE TRIGGER IF NOT EXISTS attachment_removals_no_replace BEFORE INSERT ON attachment_removals
WHEN EXISTS(SELECT 1 FROM attachment_removals WHERE id=NEW.id OR attachment_id=NEW.attachment_id)
BEGIN SELECT RAISE(ABORT,'attachment removal receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS message_attachments_no_replace BEFORE INSERT ON message_attachments
WHEN EXISTS(SELECT 1 FROM message_attachments WHERE message_id=NEW.message_id
 AND (position=NEW.position OR attachment_id=NEW.attachment_id))
BEGIN SELECT RAISE(ABORT,'message attachment bindings are immutable'); END;
"""
