"""The table of retired memory proposals, kept only so forgetting scrubs old rows.

A memory proposal was an exact text change to one MemoryGate record at a known revision, which
the owner applied or rejected through `/memory-proposals` (kind `memory` in the Inbox). No Pi
code, dashboard screen or CLI command ever created or answered one; only a caller holding Pi's
admin key could. The owner corrects memory himself through `/memory/corrections`
(`pi.memory.edits`), and Conker's own "remember" is a change request, so the routes and the
Inbox item are gone (2026-11-12, docs/inbox-kinds.md merge 2). Installs made before then may
still hold rows with a memory's old text, the proposed text and a reason. Dropping the table
would need a migration, so it stays, nothing writes or lists it, and forgetting a chat or the
memory itself scrubs those fields.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_proposals (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 payload_hash TEXT NOT NULL, memory_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 expected_revision INTEGER NOT NULL, baseline TEXT, desired TEXT, reason TEXT,
 basis TEXT NOT NULL, source_ids TEXT NOT NULL, state TEXT NOT NULL,
 receipt TEXT, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS memory_proposals_no_delete BEFORE DELETE ON memory_proposals
BEGIN SELECT RAISE(ABORT,'proposal identities are permanent'); END;
CREATE TRIGGER IF NOT EXISTS memory_proposals_no_replace BEFORE INSERT ON memory_proposals
WHEN EXISTS(SELECT 1 FROM memory_proposals WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'proposal identities are permanent'); END;
"""
FIXED = """
CREATE TRIGGER IF NOT EXISTS memory_proposals_fixed BEFORE UPDATE ON memory_proposals
WHEN NEW.id IS NOT OLD.id OR NEW.session_id IS NOT OLD.session_id
 OR NEW.payload_hash IS NOT OLD.payload_hash OR NEW.memory_id IS NOT OLD.memory_id
 OR NEW.agent_id IS NOT OLD.agent_id OR NEW.expected_revision IS NOT OLD.expected_revision
 OR NEW.baseline IS NOT OLD.baseline OR NEW.desired IS NOT OLD.desired
 OR NEW.reason IS NOT OLD.reason OR NEW.basis IS NOT OLD.basis
 OR NEW.source_ids IS NOT OLD.source_ids OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.state IN ('applied','rejected','conflict','failed','forgotten'))
BEGIN SELECT RAISE(ABORT,'reviewed proposal is immutable'); END;
"""
SCHEMA += FIXED

SCRUB = (
    "UPDATE memory_proposals SET baseline=NULL,desired=NULL,reason=NULL,"
    "payload_hash='',source_ids='[]',receipt=NULL,state='forgotten' WHERE "
)


def redact(db, sessions):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_proposals'").fetchone():
        return
    db.execute("DROP TRIGGER memory_proposals_fixed")
    for session in sessions:
        db.execute(SCRUB + "session_id=?", (session,))
    db.execute(FIXED)


def redact_memory(db, agent_id, memory_id):
    """A verified memory forget scrubs every proposal that quoted or targeted that memory."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_proposals'").fetchone():
        return
    db.execute("DROP TRIGGER memory_proposals_fixed")
    db.execute(SCRUB + "agent_id=? AND memory_id=?", (agent_id, memory_id))
    db.execute(FIXED)
