"""Owner-reviewed fork summaries with retained versions; never edit transcripts."""

import time

from pydantic import Field

from . import context_controls as controls

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_summary_versions (
 session_id TEXT NOT NULL REFERENCES sessions(id), revision INTEGER NOT NULL,
 summary TEXT, source_session_id TEXT, created_at REAL NOT NULL,
 restored_from INTEGER, PRIMARY KEY(session_id,revision)
);
CREATE TRIGGER IF NOT EXISTS summary_version_no_update BEFORE UPDATE ON context_summary_versions
BEGIN SELECT RAISE(ABORT,'summary version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS summary_version_no_replace BEFORE INSERT ON context_summary_versions
WHEN EXISTS(SELECT 1 FROM context_summary_versions
 WHERE session_id=NEW.session_id AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'summary version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS capture_automatic_summary AFTER UPDATE OF summary ON sessions
WHEN NEW.status != 'forgotten' AND NEW.summary IS NOT OLD.summary
 AND NOT EXISTS(SELECT 1 FROM context_summary_versions
 WHERE session_id=NEW.id AND revision=(SELECT MAX(revision) FROM context_summary_versions
 WHERE session_id=NEW.id) AND summary IS NEW.summary)
BEGIN
 INSERT INTO context_summary_versions
 SELECT NEW.id,0,OLD.summary,OLD.parent_id,strftime('%s','now'),NULL
 WHERE NOT EXISTS(SELECT 1 FROM context_summary_versions WHERE session_id=NEW.id);
 INSERT INTO context_summary_versions
 SELECT NEW.id,MAX(revision)+1,NEW.summary,NEW.id,strftime('%s','now'),NULL
 FROM context_summary_versions WHERE session_id=NEW.id;
END;
"""


class Edit(controls.Strict):
    expected_revision: int = Field(ge=0)
    summary: str = Field(max_length=16000)


class Restore(controls.Strict):
    expected_revision: int = Field(ge=0)
    revision: int = Field(ge=0)


def _current(db, identity):
    controls._session(db, identity)
    session = db.execute(
        "SELECT summary,parent_id FROM sessions WHERE id=?", (identity,)
    ).fetchone()
    latest = db.execute(
        "SELECT revision,source_session_id FROM context_summary_versions WHERE session_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (identity,),
    ).fetchone()
    return {
        "revision": latest["revision"] if latest else 0,
        "summary": session["summary"],
        "source_session_id": latest["source_session_id"] if latest else session["parent_id"],
    }


def load(store, identity):
    with store._connect() as db:
        value = _current(db, identity)
        value["versions"] = [
            dict(row)
            for row in db.execute(
                "SELECT revision,summary,source_session_id,created_at,restored_from "
                "FROM context_summary_versions WHERE session_id=? ORDER BY revision DESC LIMIT 100",
                (identity,),
            )
        ]
        return value


def save(store, identity, body):
    body = type(body).model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _current(db, identity)
        if current["revision"] != body.expected_revision:
            raise controls.ContextError(
                "revision_conflict", "Summary changed; reload before saving."
            )
        from . import submissions

        if (
            submissions._busy(db, identity)
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                (identity,),
            ).fetchone()
        ):
            raise controls.ContextError(
                "session_busy", "Finish current work before changing its summary."
            )
        if current["revision"] == 0:
            db.execute(
                "INSERT OR IGNORE INTO context_summary_versions VALUES(?,0,?,?,?,NULL)",
                (identity, current["summary"], current["source_session_id"], time.time()),
            )
        restored = body.revision if isinstance(body, Restore) else None
        source = current["source_session_id"]
        if isinstance(body, Restore):
            previous = db.execute(
                "SELECT summary,source_session_id FROM context_summary_versions "
                "WHERE session_id=? AND revision=?",
                (identity, body.revision),
            ).fetchone()
            if previous is None:
                raise controls.ContextError("missing_revision", "Summary version unavailable.")
            text = previous[0]
            source = previous[1]
        else:
            text = body.summary
        revision = current["revision"] + 1
        db.execute(
            "INSERT INTO context_summary_versions VALUES(?,?,?,?,?,?)",
            (identity, revision, text, source, time.time(), restored),
        )
        db.execute("UPDATE sessions SET summary=? WHERE id=?", (text, identity))
        db.commit()
    return load(store, identity)


def redact(db, session_ids):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='context_summary_versions'"
    ).fetchone():
        return
    for identity in session_ids:
        db.execute(
            "DELETE FROM context_summary_versions WHERE session_id=? OR source_session_id=?",
            (identity, identity),
        )
