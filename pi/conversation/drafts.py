"""Owner draft storage; drafts never enter model context or memory ingestion."""

import json
import time
from typing import Literal

from pydantic import Field, field_validator

from pi.core.errors import ServiceError
from pi.core.models import Strict

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_drafts (
 session_id TEXT NOT NULL REFERENCES sessions(id), scope TEXT NOT NULL,
 revision INTEGER NOT NULL, text TEXT NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(session_id,scope)
);
CREATE TABLE IF NOT EXISTS submitted_drafts (
 request_id TEXT PRIMARY KEY REFERENCES turn_submissions(request_id),
 session_id TEXT NOT NULL, scope TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_research (
 session_id TEXT NOT NULL, scope TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('web','deep')),
 PRIMARY KEY(session_id,scope),
 FOREIGN KEY(session_id,scope) REFERENCES conversation_drafts(session_id,scope) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS draft_options (
 session_id TEXT NOT NULL, scope TEXT NOT NULL, options TEXT NOT NULL,
 PRIMARY KEY(session_id,scope),
 FOREIGN KEY(session_id,scope) REFERENCES conversation_drafts(session_id,scope) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS new_conversation_draft (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
 payload TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_starts (
 request_id TEXT PRIMARY KEY, source_revision INTEGER NOT NULL,
 session_id TEXT NOT NULL REFERENCES sessions(id), created_at REAL NOT NULL
);
"""


class Intent(Strict):
    text: str = Field(max_length=16000)
    research_mode: Literal["off", "web", "deep"] = "off"
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    skill_ids: list[str] = Field(default_factory=list, max_length=3)
    think: bool = False

    @field_validator("text", "model_id")
    @classmethod
    def unicode(cls, value):
        if value is not None:
            value.encode("utf-8")
        return value

    @field_validator("skill_ids")
    @classmethod
    def skills(cls, value):
        from pi.agents.skills import validate_ids

        validate_ids(value)
        return value


class Save(Intent):
    expected_revision: int = Field(ge=0)


class NewSave(Save):
    title: str = Field(default="", max_length=1024)
    agent_id: str | None = Field(default=None, pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    privacy: dict[Literal["memoryDisabled", "harnessDisabled"], bool] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def title_unicode(cls, value):
        value.encode("utf-8")
        return value


class Start(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    expected_revision: int = Field(ge=1)


class DraftError(ServiceError, ValueError):
    def __init__(self, message, code="draft_conflict", status=409):
        super().__init__(code, message, status)


def _bounded(body):
    if len(json.dumps(body.model_dump(), ensure_ascii=False).encode("utf-8")) > 65536:
        raise DraftError("Draft exceeds the request size limit.", "draft_too_large", 413)
    return body


def _options(body):
    return {key: getattr(body, key) for key in ("model_id", "skill_ids", "think")}


def _scope(db, session_id, task_id):
    session = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if session is None or session[0] == "forgotten":
        raise DraftError("Conversation unavailable.", "draft_unavailable", 404)
    if task_id is None:
        return "chat"
    task = db.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None or task[0] != session_id:
        raise DraftError("Task does not belong to this conversation.", "draft_unavailable", 404)
    return "task:" + task_id


def _read(db, session_id, scope):
    row = db.execute(
        "SELECT revision,text,updated_at FROM conversation_drafts WHERE session_id=? AND scope=?",
        (session_id, scope),
    ).fetchone()
    result = dict(row) if row else {"revision": 0, "text": "", "updated_at": None}
    mode = db.execute(
        "SELECT mode FROM draft_research WHERE session_id=? AND scope=?", (session_id, scope)
    ).fetchone()
    result["research_mode"] = mode[0] if mode else "off"
    options = db.execute(
        "SELECT options FROM draft_options WHERE session_id=? AND scope=?", (session_id, scope)
    ).fetchone()
    result.update(json.loads(options[0]) if options else _options(Intent(text="")))
    result.update(
        schemaVersion=1,
        session_id=session_id,
        task_id=scope[5:] if scope.startswith("task:") else None,
        content_status="available",
    )
    return result


def load(store, session_id, task_id=None):
    with store._connect() as db:
        db.execute("BEGIN")
        return _read(db, session_id, _scope(db, session_id, task_id))


def save(store, session_id, body, task_id=None):
    body = _bounded(Save.model_validate(body.model_dump()))
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        scope = _scope(db, session_id, task_id)
        current = _read(db, session_id, scope)
        if current["revision"] != body.expected_revision:
            raise DraftError("Draft changed in another window; reload before saving.")
        _write(db, session_id, scope, body)
        result = _read(db, session_id, scope)
        db.commit()
        return result


def _write(db, session_id, scope, body):
    # Empty drafts keep their revision to prevent stale writers resurrecting text.
    db.execute(
        "INSERT INTO conversation_drafts VALUES(?,?,?,?,?) "
        "ON CONFLICT(session_id,scope) DO UPDATE SET "
        "revision=excluded.revision,text=excluded.text,updated_at=excluded.updated_at",
        (session_id, scope, body.expected_revision + 1, body.text, time.time()),
    )
    db.execute("DELETE FROM draft_research WHERE session_id=? AND scope=?", (session_id, scope))
    if body.research_mode != "off":
        db.execute(
            "INSERT INTO draft_research VALUES(?,?,?)", (session_id, scope, body.research_mode)
        )
    db.execute(
        "INSERT INTO draft_options VALUES(?,?,?) ON CONFLICT(session_id,scope) "
        "DO UPDATE SET options=excluded.options",
        (session_id, scope, json.dumps(_options(body))),
    )


def _new(db):
    row = db.execute("SELECT * FROM new_conversation_draft WHERE singleton=1").fetchone()
    payload = (
        json.loads(row["payload"])
        if row
        else NewSave(expected_revision=0, text="").model_dump(exclude={"expected_revision"})
    )
    return {
        "schemaVersion": 1,
        "session_id": None,
        "task_id": None,
        "content_status": "available",
        "revision": row["revision"] if row else 0,
        "updated_at": row["updated_at"] if row else None,
        **payload,
    }


def load_new(store):
    with store._connect() as db:
        db.execute("BEGIN")
        return _new(db)


def _write_new(db, body):
    db.execute(
        "INSERT INTO new_conversation_draft VALUES(1,?,?,?) ON CONFLICT(singleton) "
        "DO UPDATE SET revision=excluded.revision,payload=excluded.payload,updated_at=excluded.updated_at",
        (
            body.expected_revision + 1,
            json.dumps(body.model_dump(exclude={"expected_revision"})),
            time.time(),
        ),
    )


def save_new(store, body):
    body = _bounded(NewSave.model_validate(body.model_dump()))
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if _new(db)["revision"] != body.expected_revision:
            raise DraftError("Draft changed in another window; reload before saving.")
        _write_new(db, body)
        result = _new(db)
        db.commit()
        return result


def _start_view(db, row):
    available = (
        db.execute("SELECT status FROM sessions WHERE id=?", (row["session_id"],)).fetchone()[0]
        != "forgotten"
    )
    return {
        "schemaVersion": 1,
        "request_id": row["request_id"],
        "session_id": row["session_id"],
        "content_status": "available" if available else "forgotten",
        "source_revision": row["source_revision"] if available else None,
        "draft_revision": 1 if available else None,
    }


def start(store, body):
    body = Start.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT * FROM draft_starts WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if prior:
            if prior["source_revision"] != body.expected_revision:
                raise DraftError("This start identity was already used.", "request_conflict")
            return _start_view(db, prior)
        current = _new(db)
        if current["revision"] != body.expected_revision:
            raise DraftError("Draft changed in another window; reload before starting.")
        if not current["text"].strip():
            raise DraftError("Write a message before starting a chat.", "draft_empty", 422)
        sid = store._create_session(
            db, title=current["title"], agent_id=current["agent_id"], privacy=current["privacy"]
        )
        _write(
            db,
            sid,
            "chat",
            Save(expected_revision=0, **{key: current[key] for key in Intent.model_fields}),
        )
        db.execute(
            "INSERT INTO draft_starts VALUES(?,?,?,?)",
            (body.request_id, body.expected_revision, sid, time.time()),
        )
        _write_new(db, NewSave(expected_revision=body.expected_revision, text=""))
        result = _start_view(
            db,
            db.execute(
                "SELECT * FROM draft_starts WHERE request_id=?", (body.request_id,)
            ).fetchone(),
        )
        db.commit()
        return result


def get_start(store, request_id):
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM draft_starts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise DraftError("Chat start not found.", "draft_start_not_found", 404)
        return _start_view(db, row)


def redact(db, session_ids):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_drafts'"
    ).fetchone():
        return
    for identity in session_ids:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='draft_options'").fetchone():
            db.execute("DELETE FROM draft_options WHERE session_id=?", (identity,))
        db.execute("DELETE FROM draft_research WHERE session_id=?", (identity,))
        db.execute("DELETE FROM conversation_drafts WHERE session_id=?", (identity,))
        db.execute("DELETE FROM submitted_drafts WHERE session_id=?", (identity,))


def reserve(
    db,
    request_id,
    session_id,
    task_id,
    revision,
    text,
    research_mode="off",
    model_id=None,
    skill_ids=None,
    think=False,
):
    scope = _scope(db, session_id, task_id)
    draft = _read(db, session_id, scope)
    if (
        type(revision) is not int
        or revision < 1
        or draft["revision"] != revision
        or draft["text"] != text
        or draft.get("research_mode", "off") != research_mode
        or draft["model_id"] != model_id
        or draft["skill_ids"] != (skill_ids or [])
        or draft["think"] != think
    ):
        raise DraftError("Submitted text or options no longer matches the saved draft revision.")
    db.execute(
        "INSERT INTO submitted_drafts VALUES(?,?,?,?)", (request_id, session_id, scope, revision)
    )


def consume(db, request_id):
    row = db.execute("SELECT * FROM submitted_drafts WHERE request_id=?", (request_id,)).fetchone()
    if row:
        current = _read(db, row["session_id"], row["scope"])
        if current["revision"] == row["revision"]:
            db.execute(
                "DELETE FROM draft_options WHERE session_id=? AND scope=?",
                (row["session_id"], row["scope"]),
            )
            db.execute(
                "DELETE FROM draft_research WHERE session_id=? AND scope=?",
                (row["session_id"], row["scope"]),
            )
        db.execute(
            "UPDATE conversation_drafts SET text='',revision=revision+1,updated_at=? "
            "WHERE session_id=? AND scope=? AND revision=?",
            (time.time(), row["session_id"], row["scope"], row["revision"]),
        )
