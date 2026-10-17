"""Output settings: the answer's format, length and language for one chat. Auto leaves it to the model."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pi.models.providers import Message

AUTO = {"format": "auto", "length": "auto", "language": "auto"}
# "auto" or a BCP 47 tag such as "ru" or "en-GB".
LANGUAGE = r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3})$"
FORMATS = {
    "text": "plain prose",
    "list": "a list",
    "table": "a table",
    "document": "a structured document with headings",
    "code": "code, with only brief explanation",
    "json": "only valid JSON",
}
LENGTHS = {
    "short": "short, a few sentences",
    "medium": "medium, a few paragraphs",
    "long": "long and thorough",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Output(Strict):
    format: Literal["auto", "text", "list", "table", "document", "code", "json"] = "auto"
    length: Literal["auto", "short", "medium", "long"] = "auto"
    language: str = Field(default="auto", max_length=35, pattern=LANGUAGE)


class Update(Strict):
    expected_revision: int = Field(ge=0)
    output: Output


class OutputError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


SCHEMA = """
CREATE TABLE IF NOT EXISTS session_output (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), revision INTEGER NOT NULL, output TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS inherit_session_output AFTER INSERT ON sessions
WHEN NEW.parent_id IS NOT NULL
BEGIN
 INSERT INTO session_output SELECT NEW.id,revision,output FROM session_output WHERE session_id=NEW.parent_id;
END;
"""


def _session(db, session_id):
    row = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if (
        row is None
        or row["status"] == "forgotten"
        or db.execute(
            "SELECT 1 FROM forgotten_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    ):
        raise OutputError("not_found", "Conversation unavailable.", 404)
    return row["status"]


def _stored(db, session_id):
    row = db.execute(
        "SELECT revision,output FROM session_output WHERE session_id=?", (session_id,)
    ).fetchone()
    return (row["revision"], json.loads(row["output"])) if row else (0, dict(AUTO))


def load(store, session_id) -> dict:
    with store._connect() as db:
        _session(db, session_id)
        revision, output = _stored(db, session_id)
    return {"revision": revision, "output": output}


def save(store, session_id, body: Update) -> dict:
    body = Update.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if _session(db, session_id) != "open":
            raise OutputError("session_closed", "Change output settings on an open chat.", 409)
        revision, _ = _stored(db, session_id)
        if revision != body.expected_revision:
            raise OutputError(
                "revision_conflict", "Output settings changed; reload before saving.", 409
            )
        db.execute(
            "INSERT INTO session_output VALUES (?,?,?) ON CONFLICT(session_id) "
            "DO UPDATE SET revision=excluded.revision,output=excluded.output",
            (session_id, revision + 1, body.output.model_dump_json()),
        )
        db.commit()
    return {"revision": revision + 1, "output": body.output.model_dump()}


def snapshot(db, session_id) -> dict | None:
    """The chat's saved output for a turn snapshot; None when unsaved or all Auto."""
    _, output = _stored(db, session_id)
    return None if output == AUTO else output


def messages(execution) -> list[Message]:
    output = execution.get("output")
    if not output:
        return []
    parts = []
    if output.get("format", "auto") != "auto":
        parts.append(f"Answer format: {FORMATS[output['format']]}.")
    if output.get("length", "auto") != "auto":
        parts.append(f"Answer length: {LENGTHS[output['length']]}.")
    if output.get("language", "auto") != "auto":
        parts.append(f"Answer language: {output['language']}, unless the owner asks otherwise.")
    if not parts:
        return []
    return [Message("system", "Owner output settings for this chat. " + " ".join(parts))]
