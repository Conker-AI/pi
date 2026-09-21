"""Durable submission identities and exact, immutable turn/message associations.

Only the caller that inserts a receipt may prepare/execute it. Replays inspect;
they never restart a model or an action. Provider calls stay outside transactions.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from . import session_settings, tasks

UNRESOLVED = ("('running','awaiting_approval','awaiting_budget','acted_no_reply',"
              "'action_in_progress','outcome_unknown')")

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


class SubmissionError(tasks.TaskError):
    pass


def append(db, session_id, role, content, *, turn_id=None, purpose=None, action_id=None):
    """Caller owns the transaction, including the memory outbox trigger and association."""
    if role == "user" and (not isinstance(content, str) or len(content) > 16000):
        raise ValueError("Send user text in messages of at most 16000 characters.")
    if bool(turn_id) != bool(purpose) or (action_id and not turn_id):
        raise ValueError("Turn and purpose must be supplied together.")
    identity, now = "msg_" + uuid.uuid4().hex[:16], time.time()
    seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?",
                     (session_id,)).fetchone()[0]
    db.execute("INSERT INTO messages(id,session_id,seq,role,content,created_at) "
               "VALUES(?,?,?,?,?,?)",
               (identity, session_id, seq, role, json.dumps(content, ensure_ascii=False), now))
    if turn_id:
        db.execute("INSERT INTO turn_messages VALUES(?,?,?,?)",
                   (identity, turn_id, purpose, action_id))
    return {"id": identity, "session_id": session_id, "seq": seq, "role": role,
            "content": content, "created_at": now}


def message_refs(db, turn_id):
    return [dict(row) for row in db.execute(
        "SELECT tm.message_id,tm.purpose,tm.action_id,m.seq FROM turn_messages tm "
        "JOIN messages m ON m.id=tm.message_id WHERE tm.turn_id=? ORDER BY m.seq", (turn_id,)
    )]


def _view(db, row):
    available = row["state"] != "forgotten" and not db.execute(
        "SELECT 1 FROM forgotten_sessions WHERE session_id IN (?,?)",
        (row["requested_session_id"], row["effective_session_id"]),
    ).fetchone()
    turn = db.execute("SELECT status,acted FROM turns WHERE id=?",
                      (row["turn_id"],)).fetchone()
    refs = message_refs(db, row["turn_id"]) if row["turn_id"] else []
    return {
        "request_id": row["request_id"], "requested_session_id": row["requested_session_id"],
        "effective_session_id": row["effective_session_id"], "turn_id": row["turn_id"],
        "task_id": row["task_id"], "input_message_id": row["input_message_id"],
        "final_message_id": next((ref["message_id"] for ref in refs
                                   if ref["purpose"] == "final"), None),
        "state": row["state"], "status": turn["status"] if turn else row["state"],
        "acted": bool(turn["acted"]) if turn else False, "message_refs": refs,
        "pending_text": row["pending_text"] if available else None,
        "failure_code": row["failure_code"] if available else None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "content_status": "available" if available else "forgotten",
    }


def _row(db, request_id):
    row = db.execute("SELECT * FROM turn_submissions WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        raise SubmissionError("not_found", "Submission not found.", 404)
    return row


def get(store, request_id):
    with store._connect() as db:
        db.execute("BEGIN")
        return _view(db, _row(db, request_id))


def list_pending(store, session_id, limit=50, cursor=None):
    """Content-free pointers let a reloaded browser rediscover saved, unresolved inputs."""
    limit = min(max(limit, 1), 200)
    with store._connect() as db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise SubmissionError("not_found", "Conversation not found.", 404)
        where = ["(s.requested_session_id=? OR s.effective_session_id=?)",
                 ("(s.state IN ('preparing','preparation_failed','preparation_interrupted') "
                  f"OR (s.state='bound' AND (t.status IN {UNRESOLVED} "
                  "OR (t.status='interrupted' AND t.acted=1))))")]
        values = [session_id, session_id]
        if cursor:
            before = _row(db, cursor)
            where.append("(s.created_at,s.request_id)<(?,?)")
            values.extend((before["created_at"], cursor))
        rows = db.execute("SELECT s.*,t.status AS turn_status FROM turn_submissions s "
                          "LEFT JOIN turns t ON t.id=s.turn_id WHERE " + " AND ".join(where) +
                          " ORDER BY s.created_at DESC,s.request_id DESC LIMIT ?",
                          (*values, limit + 1)).fetchall()
        keys = ("request_id", "requested_session_id", "effective_session_id", "turn_id",
                "state", "created_at", "updated_at")
        return {"results": [{**{key: row[key] for key in keys},
                             "status": row["turn_status"] or row["state"],
                             "content_status": "available"} for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["request_id"] if len(rows) > limit else None}


def _busy(db, session_id):
    row = db.execute(f"SELECT 1 FROM turns WHERE session_id=? AND (status IN {UNRESOLVED} "
                     "OR (status='interrupted' AND acted=1))", (session_id,)).fetchone()
    return row is not None


def _task(db, task_id, revision, session_id):
    if task_id is None:
        return
    row = tasks._row(db, task_id, revision)
    if row["session_id"] != session_id:
        raise SubmissionError("foreign_task", "The task belongs to a different conversation.")
    if row["archived_at"] is not None or row["status"] in tasks.TERMINAL:
        raise SubmissionError("task_inactive",
                              "Restore and reopen the task before submitting work.")


def reserve(store, request_id, session_id, text, context, task_id=None, task_revision=None, draft_revision=None, attachment_ids=None):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
        raise SubmissionError("invalid_request",
                              "Provide a valid submission request identity.", 422)
    if not isinstance(text, str) or not 1 <= len(text) <= 16000:
        raise SubmissionError("invalid_request", "Send 1-16000 characters per message.", 422)
    if (task_id is None) != (task_revision is None):
        raise SubmissionError("invalid_task", "Task identity and revision belong together.", 422)
    if task_revision is not None and (type(task_revision) is not int or task_revision < 1):
        raise SubmissionError("invalid_task", "Provide the current task revision.", 422)
    payload = {
        "session_id": session_id, "text": text, "context": context,
        "task_id": task_id, "task_revision": task_revision,
    }
    if draft_revision is not None:
        payload["draft_revision"] = draft_revision
    if attachment_ids:
        payload["attachment_ids"] = attachment_ids
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM turn_submissions WHERE request_id=?",
                           (request_id,)).fetchone()
        if prior:
            if prior["payload_hash"] != digest:
                raise SubmissionError("request_conflict",
                                      "This submission identity was already used.")
            return _view(db, prior), False
        tasks._source(db, session_id, open_required=True)
        _task(db, task_id, task_revision, session_id)
        if _busy(db, session_id) or db.execute(
            "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
            (session_id,),
        ).fetchone():
            raise SubmissionError("session_busy", "This conversation already has work in progress.")
        head = db.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE session_id=?",
                          (session_id,)).fetchone()[0]
        now = time.time()
        db.execute("INSERT INTO turn_submissions(request_id,requested_session_id,task_id,"
                   "task_expected_revision,state,payload_hash,pending_text,history_seq,"
                   "created_at,updated_at) VALUES(?,?,?,?,'preparing',?,?,?,?,?)",
                   (request_id, session_id, task_id, task_revision, digest, text, head, now, now))
        session_settings.reserve(db, request_id, session_id)
        if attachment_ids:
            from . import attachment_turns, attachments
            try:
                attachment_turns.reserve(db, request_id, session_id, attachment_ids)
            except attachments.AttachmentError as exc:
                raise SubmissionError(exc.detail["code"], exc.detail["message"], exc.status) from exc
        if draft_revision is not None:
            from . import drafts
            try:
                drafts.reserve(db, request_id, session_id, task_id, draft_revision, text)
            except drafts.DraftError as exc:
                raise SubmissionError("draft_changed", str(exc)) from exc
        result = _view(db, _row(db, request_id))
        db.commit()
        return result, True


def fail_preparation(store, request_id, code="preparation_failed"):
    # Static codes only; provider exceptions and request text must not enter a second log.
    if code not in {"preparation_failed", "task_fork_required", "source_changed"}:
        code = "preparation_failed"
    with store._connect() as db:
        db.execute("UPDATE turn_submissions SET state='preparation_failed',failure_code=?,"
                   "updated_at=? WHERE request_id=? AND state='preparing'",
                   (code, time.time(), request_id))
        from . import attachment_turns
        attachment_turns.release(db, request_id)


def bind(store, request_id, *, fork_summary=None):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, request_id)
        if row["state"] != "preparing":
            raise SubmissionError("not_preparing", "This submission cannot start again.")
        session_id = row["requested_session_id"]
        tasks._source(db, session_id, open_required=True)
        _task(db, row["task_id"], row["task_expected_revision"], session_id)
        head = db.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE session_id=?",
                          (session_id,)).fetchone()[0]
        if head != row["history_seq"] or _busy(db, session_id):
            raise SubmissionError("source_changed", "The conversation changed during preparation.")
        now = time.time()
        if fork_summary is not None:
            if db.execute("SELECT 1 FROM attachment_reservations WHERE request_id=?", (request_id,)).fetchone():
                raise SubmissionError("attachment_fork_required", "Fork first, then upload attachments to the new conversation.")
            if row["task_id"]:
                raise SubmissionError("task_fork_required", "Task-bound turns require the original "
                                      "conversation. Create a task for the child first.")
            parent = db.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
            child = "ses_" + uuid.uuid4().hex[:16]
            db.execute("UPDATE sessions SET status='forked',closed_at=?,summary=? WHERE id=?",
                       (now, fork_summary, session_id))
            db.execute("INSERT INTO sessions(id,parent_id,title,status,created_at,summary) "
                       "VALUES(?,?,?,'open',?,?)",
                       (child, session_id, parent["title"], now, fork_summary))
            session_id = child
        turn_id = "trn_" + uuid.uuid4().hex[:16]
        db.execute("INSERT INTO turns(id,session_id,status,started_at) VALUES(?,?,'running',?)",
                   (turn_id, session_id, now))
        session_settings.bind(db, turn_id, session_id, request_id)
        message = append(db, session_id, "user", row["pending_text"],
                         turn_id=turn_id, purpose="input")
        from . import attachment_turns
        attachment_turns.bind(db, request_id, session_id, message["id"])
        if row["task_id"]:
            ids = [r[0] for r in db.execute("SELECT run_id FROM task_runs WHERE task_id=?",
                                           (row["task_id"],))]
            if len(ids) >= 100:
                raise SubmissionError("task_run_limit", "The task already links 100 runs.")
            db.execute("UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=?",
                       (now, row["task_id"]))
            tasks._set_runs(db, row["task_id"], session_id, [*ids, turn_id])
        db.execute("UPDATE turn_submissions SET effective_session_id=?,turn_id=?,"
                   "input_message_id=?,"
                   "state='bound',pending_text=NULL,updated_at=? WHERE request_id=?",
                   (session_id, turn_id, message["id"], now, request_id))
        from . import drafts
        drafts.consume(db, request_id)
        result = _view(db, _row(db, request_id))
        db.commit()
        return result


def recover_preparations(db):
    changed = db.execute("UPDATE turn_submissions SET state='preparation_interrupted',"
                      "failure_code='preparation_interrupted',updated_at=? WHERE state='preparing'",
                      (time.time(),)).rowcount
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='attachment_reservations'").fetchone():
        db.execute("DELETE FROM attachment_reservations WHERE request_id IN "
                   "(SELECT request_id FROM turn_submissions WHERE state='preparation_interrupted')")
    return changed


def redact(db, session_ids):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_submissions'").fetchone():
        return
    for session_id in session_ids:
        db.execute("UPDATE turn_submissions SET pending_text=NULL,payload_hash=NULL,"
                   "failure_code=NULL,state='forgotten' WHERE requested_session_id=? "
                   "OR effective_session_id=?", (session_id, session_id))
