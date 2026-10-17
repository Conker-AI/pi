"""Output settings: the answer's format, length and language for one chat. Auto leaves it to the model."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, Strict

FORMATS = ("auto", "text", "list", "table", "document", "code", "json")
LENGTHS = ("auto", "short", "medium", "long")
LANGUAGE = (
    r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3})$"  # "auto" or a BCP 47 tag like "ru", "en-GB"
)


class Output(Strict):  # Strict = pydantic BaseModel with ConfigDict(strict=True, extra="forbid")
    format: Literal[FORMATS] = "auto"
    length: Literal[LENGTHS] = "auto"
    language: str = Field(default="auto", max_length=35, pattern=LANGUAGE)


class Update(Strict):
    expected_revision: int = Field(ge=0)
    output: Output


class OutputError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


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


def load(store, session_id) -> dict:
    """Load output settings for a session."""
    with store._connect() as db:
        # Check if session exists and is not forgotten
        session = db.execute(
            "SELECT id, status FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise OutputError("not_found", "Conversation unavailable.", 404)

        if session["status"] == "forgotten":
            raise OutputError("not_found", "Conversation unavailable.", 404)

        # Check if in forgotten_sessions
        forgotten = db.execute(
            "SELECT 1 FROM forgotten_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if forgotten:
            raise OutputError("not_found", "Conversation unavailable.", 404)

        # Load output settings
        row = db.execute(
            "SELECT revision, output FROM session_output WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row:
            return {"revision": row["revision"], "output": row["output"]}
        else:
            return {
                "revision": 0,
                "output": {"format": "auto", "length": "auto", "language": "auto"},
            }


def save(store, session_id, body: Update) -> dict:
    """Save output settings for a session."""
    with store._connect() as db:
        # Check if session exists and is not forgotten
        session = db.execute(
            "SELECT id, status FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise OutputError("not_found", "Conversation unavailable.", 404)

        if session["status"] == "forgotten":
            raise OutputError("not_found", "Conversation unavailable.", 404)

        # Check if in forgotten_sessions
        forgotten = db.execute(
            "SELECT 1 FROM forgotten_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if forgotten:
            raise OutputError("not_found", "Conversation unavailable.", 404)

        # Check if session is open
        if session["status"] != "open":
            raise OutputError("session_closed", "Change output settings on an open chat.", 409)

        # Get current revision
        current = db.execute(
            "SELECT revision FROM session_output WHERE session_id = ?", (session_id,)
        ).fetchone()
        current_revision = current["revision"] if current else 0

        # Check revision
        if body.expected_revision != current_revision:
            raise OutputError(
                "revision_conflict", "Output settings changed; reload before saving.", 409
            )

        # Save new settings
        new_revision = current_revision + 1
        output_json = body.output.model_dump_json()

        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR REPLACE INTO session_output (session_id, revision, output) VALUES (?, ?, ?)",
                (session_id, new_revision, output_json),
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

        return {"revision": new_revision, "output": body.output.model_dump()}


def snapshot(db, session_id) -> dict | None:
    """Get the saved output, or None when unsaved or all three are "auto"."""
    row = db.execute(
        "SELECT output FROM session_output WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row:
        return None

    import json

    output = json.loads(row["output"])

    # If all values are "auto", return None
    if (
        output.get("format") == "auto"
        and output.get("length") == "auto"
        and output.get("language") == "auto"
    ):
        return None

    return output


def messages(execution) -> list:
    """Generate output setting messages, or [] if none."""
    from pi.models.providers import Message

    output = execution.get("output")
    if not output:
        return []

    parts = []

    # Format
    format_value = output.get("format")
    if format_value and format_value != "auto":
        format_descriptions = {
            "text": "plain prose",
            "list": "a list",
            "table": "a table",
            "document": "a structured document with headings",
            "code": "code, with only brief explanation",
            "json": "only valid JSON",
        }
        description = format_descriptions.get(format_value, format_value)
        parts.append(f"Answer format: {description}.")

    # Length
    length_value = output.get("length")
    if length_value and length_value != "auto":
        length_descriptions = {
            "short": "short, a few sentences",
            "medium": "medium, a few paragraphs",
            "long": "long and thorough",
        }
        description = length_descriptions.get(length_value, length_value)
        parts.append(f"Answer length: {description}.")

    # Language
    language_value = output.get("language")
    if language_value and language_value != "auto":
        parts.append(f"Answer language: {language_value}, unless the owner asks otherwise.")

    if not parts:
        return []

    text = "Owner output settings for this chat. " + " ".join(parts)
    return [Message("system", text)]
