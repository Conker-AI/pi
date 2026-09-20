"""Durable owner task metadata. Creating or updating a task never dispatches work."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Status = Literal["planned", "in_progress", "blocked", "completed", "cancelled"]
TRANSITIONS = {
    "planned": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "cancelled"},
    "blocked": {"planned", "in_progress", "cancelled"},
    "completed": {"planned"},
    "cancelled": {"planned"},
}
TERMINAL = {"completed", "cancelled"}


class TaskError(Exception):
    def __init__(self, code, message, status=409, current_revision=None):
        super().__init__(message)
        self.status = status
        self.detail = {"code": code, "message": message}
        if current_revision is not None:
            self.detail["current_revision"] = current_revision


class TaskFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: str = Field(min_length=1, max_length=1000)
    criteria: list[str] = Field(min_length=1, max_length=20)
    parent_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("outcome")
    @classmethod
    def outcome_text(cls, value):
        if not value.strip():
            raise ValueError("Provide an outcome.")
        return value.strip()

    @field_validator("criteria")
    @classmethod
    def criterion_text(cls, values):
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("Each criterion needs 1-500 characters.")
        values = [value.strip() for value in values]
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("Criteria must be distinct.")
        return values

    @field_validator("run_ids")
    @classmethod
    def distinct_runs(cls, values):
        if len(set(values)) != len(values) or any(
            not value or len(value) > 128 for value in values
        ):
            raise ValueError("Link distinct existing run identities.")
        return values


class CreateTask(TaskFields):
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    session_id: str = Field(min_length=1, max_length=128)


class UpdateTask(TaskFields):
    expected_revision: int = Field(ge=1)


class TransitionTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    status: Status
    note: str = Field(min_length=1, max_length=2000)
    completed_criterion_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("note")
    @classmethod
    def note_text(cls, value):
        if not value.strip():
            raise ValueError("Explain this status change.")
        return value.strip()


class ArchiveTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    archived: bool


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    parent_task_id TEXT REFERENCES tasks(id),
    outcome TEXT NOT NULL,
    criteria TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('planned','in_progress','blocked','completed','cancelled')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at REAL NOT NULL, updated_at REAL NOT NULL, archived_at REAL,
    status_note TEXT NOT NULL DEFAULT '',
    completed_criterion_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS task_runs (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT NOT NULL REFERENCES turns(id),
    PRIMARY KEY(task_id, run_id)
);
CREATE TABLE IF NOT EXISTS task_requests (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    payload_hash TEXT
);
CREATE INDEX IF NOT EXISTS tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS task_runs_run ON task_runs(run_id);
CREATE TABLE IF NOT EXISTS activity_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE DEFAULT ('evt_' || lower(hex(randomblob(16)))),
    kind TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    task_id TEXT REFERENCES tasks(id), run_id TEXT REFERENCES turns(id),
    action_id TEXT REFERENCES tool_actions(id),
    from_status TEXT, to_status TEXT, revision INTEGER,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON activity_events(task_id,sequence);
CREATE INDEX IF NOT EXISTS events_run ON activity_events(run_id,sequence);
CREATE INDEX IF NOT EXISTS events_session ON activity_events(session_id,sequence);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON activity_events
BEGIN SELECT RAISE(ABORT,'activity events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON activity_events
BEGIN SELECT RAISE(ABORT,'activity events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_replace BEFORE INSERT ON activity_events
WHEN EXISTS (SELECT 1 FROM activity_events WHERE id=NEW.id OR sequence=NEW.sequence)
BEGIN SELECT RAISE(ABORT,'activity events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS tasks_fixed_session BEFORE UPDATE OF session_id ON tasks
WHEN NEW.session_id != OLD.session_id
BEGIN SELECT RAISE(ABORT,'task session is fixed'); END;
CREATE TRIGGER IF NOT EXISTS tasks_created_event AFTER INSERT ON tasks
BEGIN
    INSERT INTO activity_events(kind,session_id,task_id,to_status,revision,occurred_at)
    VALUES('task_created',NEW.session_id,NEW.id,NEW.status,NEW.revision,NEW.created_at);
END;
CREATE TRIGGER IF NOT EXISTS tasks_changed_event AFTER UPDATE ON tasks
WHEN NEW.revision != OLD.revision
BEGIN
    INSERT INTO activity_events(kind,session_id,task_id,from_status,to_status,revision,occurred_at)
    VALUES(CASE WHEN NEW.status != OLD.status THEN 'task_status'
                WHEN NEW.archived_at IS NOT OLD.archived_at
                    THEN CASE WHEN NEW.archived_at IS NULL THEN 'task_restored'
                              ELSE 'task_archived' END
                ELSE 'task_updated' END,
           NEW.session_id,NEW.id,OLD.status,NEW.status,NEW.revision,NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS turns_started_event AFTER INSERT ON turns
BEGIN
    INSERT INTO activity_events(kind,session_id,run_id,to_status,occurred_at)
    VALUES('run_started',NEW.session_id,NEW.id,NEW.status,NEW.started_at);
END;
CREATE TRIGGER IF NOT EXISTS turns_changed_event AFTER UPDATE OF status ON turns
WHEN NEW.status != OLD.status
BEGIN
    INSERT INTO activity_events(kind,session_id,run_id,from_status,to_status,occurred_at)
    VALUES('run_status',NEW.session_id,NEW.id,OLD.status,NEW.status,
           (julianday('now') - 2440587.5) * 86400.0);
END;
CREATE TRIGGER IF NOT EXISTS actions_started_event AFTER INSERT ON tool_actions
BEGIN
    INSERT INTO activity_events(kind,session_id,run_id,action_id,to_status,occurred_at)
    SELECT 'action_started',session_id,NEW.turn_id,NEW.id,NEW.state,NEW.created_at
    FROM turns WHERE id=NEW.turn_id;
END;
CREATE TRIGGER IF NOT EXISTS actions_changed_event AFTER UPDATE OF state ON tool_actions
WHEN NEW.state != OLD.state
BEGIN
    INSERT INTO activity_events(kind,session_id,run_id,action_id,from_status,to_status,occurred_at)
    SELECT 'action_status',session_id,NEW.turn_id,NEW.id,OLD.state,NEW.state,
           (julianday('now') - 2440587.5) * 86400.0
    FROM turns WHERE id=NEW.turn_id;
END;
"""


def _source(db, session_id, *, open_required=False):
    row = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row or row["status"] == "forgotten" or db.execute(
        "SELECT 1 FROM forgotten_sessions WHERE session_id=?", (session_id,)
    ).fetchone():
        raise TaskError("source_unavailable", "The source conversation is unavailable.")
    if open_required and row["status"] != "open":
        raise TaskError("source_closed", "Choose an open conversation for new work.")


def _row(db, task_id, revision=None):
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise TaskError("not_found", "Task not found.", 404)
    if revision is not None and row["revision"] != revision:
        raise TaskError("revision_conflict", "Reload the task before saving.",
                        current_revision=row["revision"])
    return row


def _links(db, session_id, parent_id, run_ids, task_id=None):
    visited = {task_id}
    current = parent_id
    while current:
        if current in visited:
            raise TaskError("parent_cycle", "A task cannot be its own ancestor.")
        visited.add(current)
        if len(visited) > 100:
            raise TaskError("parent_depth", "Task ancestry is limited to 100 levels.")
        parent = _row(db, current)
        if parent["session_id"] != session_id:
            raise TaskError("foreign_parent", "Parent tasks must use the same conversation.")
        if parent["archived_at"] is not None or parent["status"] in TERMINAL:
            raise TaskError("parent_inactive", "Choose an active parent task.")
        current = parent["parent_task_id"]
    for run_id in run_ids:
        turn = db.execute("SELECT session_id FROM turns WHERE id=?", (run_id,)).fetchone()
        if not turn or turn["session_id"] != session_id:
            raise TaskError("invalid_run", "Link an existing run from this conversation.")


def _set_runs(db, task_id, session_id, run_ids):
    previous = {r[0] for r in db.execute(
        "SELECT run_id FROM task_runs WHERE task_id=?", (task_id,)
    )}
    for run_id in sorted(previous - set(run_ids)):
        db.execute("DELETE FROM task_runs WHERE task_id=? AND run_id=?", (task_id, run_id))
        db.execute("INSERT INTO activity_events(kind,session_id,task_id,run_id,occurred_at) "
                   "VALUES('run_unlinked',?,?,?,?)", (session_id, task_id, run_id, time.time()))
    for run_id in sorted(set(run_ids) - previous):
        db.execute("INSERT INTO task_runs VALUES(?,?)", (task_id, run_id))
        db.execute("INSERT INTO activity_events(kind,session_id,task_id,run_id,occurred_at) "
                   "VALUES('run_linked',?,?,?,?)", (session_id, task_id, run_id, time.time()))


def _no_active_children(db, task_id):
    if db.execute("SELECT 1 FROM tasks WHERE parent_task_id=? "
                  "AND status NOT IN ('completed','cancelled')", (task_id,)).fetchone():
        raise TaskError("active_children", "Resolve active child tasks first.")


def _view(db, row):
    value = dict(row)
    available = db.execute("SELECT 1 FROM sessions WHERE id=? AND status!='forgotten' "
                           "AND id NOT IN (SELECT session_id FROM forgotten_sessions)",
                           (row["session_id"],)).fetchone() is not None
    value.update(agent_id="companion", status_source="owner", provenance="recorded",
                 content_status="available" if available else "forgotten")
    value["criteria"] = json.loads(value["criteria"]) if available else []
    value["completed_criterion_ids"] = (
        json.loads(value["completed_criterion_ids"]) if available else []
    )
    if not available:
        value["outcome"] = value["status_note"] = ""
    value["run_ids"] = [r[0] for r in db.execute(
        "SELECT run_id FROM task_runs WHERE task_id=? ORDER BY run_id", (row["id"],)
    )]
    events = db.execute("SELECT * FROM activity_events WHERE task_id=? "
                        "ORDER BY sequence DESC LIMIT 101", (row["id"],)).fetchall()
    value["changes_truncated"] = len(events) > 100
    value["changes"] = [{**dict(event), "content_status": value["content_status"]}
                        for event in reversed(events[:100])]
    return value


def get(store, task_id):
    with store._connect() as db:
        db.execute("BEGIN")
        return _view(db, _row(db, task_id))


def by_request(store, request_id):
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT task_id FROM task_requests WHERE request_id=?",
                         (request_id,)).fetchone()
        if not row:
            raise TaskError("not_found", "This task request has not been recorded.", 404)
        return _view(db, _row(db, row[0]))


def list_tasks(store, limit=50, cursor=None, session_id=None):
    limit = min(max(limit, 1), 200)
    with store._connect() as db:
        db.execute("BEGIN")
        where, values = [], []
        if cursor:
            before = _row(db, cursor)
            where.append("(created_at,id) < (?,?)")
            values.extend((before["created_at"], cursor))
        if session_id:
            where.append("session_id=?")
            values.append(session_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = db.execute("SELECT * FROM tasks" + clause +
                          " ORDER BY created_at DESC,id DESC LIMIT ?",
                          (*values, min(max(limit, 1), 200) + 1)).fetchall()
        page = rows[:limit]
        return {"results": [_view(db, row) for row in page],
                "next_cursor": page[-1]["id"] if len(rows) > limit else None}


def create(store, body: CreateTask):
    payload = body.model_dump(exclude={"request_id"})
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM task_requests WHERE request_id=?",
                           (body.request_id,)).fetchone()
        if prior:
            if prior["payload_hash"] != digest:
                raise TaskError("request_conflict", "This request identity was already used.")
            return _view(db, _row(db, prior["task_id"]))
        _source(db, body.session_id, open_required=True)
        _links(db, body.session_id, body.parent_task_id, body.run_ids)
        identity, now = "tsk_" + uuid.uuid4().hex, time.time()
        criteria = [{"id": "crit_" + uuid.uuid4().hex, "text": text}
                    for text in body.criteria]
        db.execute("INSERT INTO tasks(id,session_id,parent_task_id,outcome,criteria,status,"
                   "revision,created_at,updated_at) VALUES(?,?,?,?,?,'planned',1,?,?)",
                   (identity, body.session_id, body.parent_task_id, body.outcome,
                    json.dumps(criteria, ensure_ascii=False), now, now))
        db.execute("INSERT INTO task_requests VALUES(?,?,?)",
                   (body.request_id, identity, digest))
        _set_runs(db, identity, body.session_id, body.run_ids)
        result = _view(db, _row(db, identity))
        db.commit()
        return result


def update(store, task_id, body: UpdateTask):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, task_id, body.expected_revision)
        _source(db, row["session_id"])
        if row["archived_at"] is not None or row["status"] in TERMINAL:
            raise TaskError("task_inactive", "Restore and reopen the task before editing it.")
        _links(db, row["session_id"], body.parent_task_id, body.run_ids, task_id)
        old = {item["text"]: item for item in json.loads(row["criteria"])}
        criteria = [old.get(text) or {"id": "crit_" + uuid.uuid4().hex, "text": text}
                    for text in body.criteria]
        db.execute("UPDATE tasks SET outcome=?,criteria=?,parent_task_id=?,revision=revision+1,"
                   "updated_at=?,completed_criterion_ids='[]' WHERE id=?",
                   (body.outcome, json.dumps(criteria, ensure_ascii=False), body.parent_task_id,
                    time.time(), task_id))
        _set_runs(db, task_id, row["session_id"], body.run_ids)
        result = _view(db, _row(db, task_id))
        db.commit()
        return result


def transition(store, task_id, body: TransitionTask):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, task_id, body.expected_revision)
        _source(db, row["session_id"])
        if row["archived_at"] is not None or body.status not in TRANSITIONS[row["status"]]:
            raise TaskError("invalid_transition", "Restore or reopen this task before changing it.")
        criteria = {item["id"] for item in json.loads(row["criteria"])}
        reviewed = body.completed_criterion_ids
        if body.status == "completed":
            if len(reviewed) != len(criteria) or set(reviewed) != criteria:
                raise TaskError("review_required", "Review every current completion criterion.")
        elif reviewed:
            raise TaskError("invalid_review", "Criterion review belongs to task completion.")
        if body.status in TERMINAL:
            _no_active_children(db, task_id)
        else:
            _source(db, row["session_id"], open_required=True)
            _links(db, row["session_id"], row["parent_task_id"], [], task_id)
        db.execute("UPDATE tasks SET status=?,status_note=?,completed_criterion_ids=?,"
                   "revision=revision+1,updated_at=? WHERE id=?",
                   (body.status, body.note, json.dumps(reviewed), time.time(), task_id))
        result = _view(db, _row(db, task_id))
        db.commit()
        return result


def archive(store, task_id, body: ArchiveTask):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, task_id, body.expected_revision)
        _source(db, row["session_id"])
        if bool(row["archived_at"] is not None) == body.archived:
            return _view(db, row)
        if body.archived:
            if row["status"] not in TERMINAL:
                raise TaskError("task_active", "Complete or cancel the task before archiving it.")
            _no_active_children(db, task_id)
        now = time.time()
        db.execute("UPDATE tasks SET archived_at=?,revision=revision+1,updated_at=? WHERE id=?",
                   (now if body.archived else None, now, task_id))
        result = _view(db, _row(db, task_id))
        db.commit()
        return result


def redact(db, session_ids):
    """Called only within offline forgetting's exclusive transaction; events contain no text."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
        return
    for session_id in session_ids:
        db.execute("UPDATE tasks SET outcome='',criteria='[]',status_note='',"
                   "completed_criterion_ids='[]' WHERE session_id=?", (session_id,))
        db.execute("UPDATE task_requests SET payload_hash=NULL WHERE task_id IN "
                   "(SELECT id FROM tasks WHERE session_id=?)", (session_id,))
