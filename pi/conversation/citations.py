"""Supplied assistant evidence, immutable beside exact message provenance.

A citation is a provider-supplied reference, never independent verification and
never proof that a retrieved memory was actually cited. Links are inert metadata.
"""

from __future__ import annotations

import json
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=500)
    href: str | None = Field(default=None, max_length=2048)
    excerpt: str | None = Field(default=None, max_length=8000)

    @field_validator("href")
    @classmethod
    def inert_link(cls, value):
        if value:
            parts = urlsplit(value)
            if (
                parts.scheme not in ("https", "http")
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or any(ord(c) < 33 for c in value)
                or "\\" in value
            ):
                raise ValueError("Citations require complete HTTP(S) links without credentials.")
            _ = parts.port
        return value


def normalize(value):
    citations = TypeAdapter(Annotated[list[Citation], Field(max_length=100)]).validate_python(
        [] if value is None else value, strict=True
    )
    result = [item.model_dump(exclude_none=True) for item in citations]
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len({item["id"] for item in result}) != len(result):
        raise ValueError("Citation identities must be distinct.")
    if len(serialized.encode("utf-16-le")) // 2 > 100_000:
        raise ValueError("Citations exceed 100,000 serialized characters.")
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS message_citations (
 message_id TEXT PRIMARY KEY REFERENCES messages(id), body TEXT,
 redacted INTEGER NOT NULL DEFAULT 0 CHECK(redacted IN (0,1)),
 CHECK((body IS NOT NULL AND redacted=0) OR (body IS NULL AND redacted=1))
);
CREATE TRIGGER IF NOT EXISTS message_citations_no_replace BEFORE INSERT ON message_citations
WHEN EXISTS(SELECT 1 FROM message_citations WHERE message_id=NEW.message_id)
BEGIN SELECT RAISE(ABORT,'message citations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS message_citations_valid BEFORE INSERT ON message_citations
WHEN NOT EXISTS (
 SELECT 1 FROM messages m JOIN turn_messages tm ON tm.message_id=m.id
 JOIN turns t ON t.id=tm.turn_id
 WHERE m.id=NEW.message_id AND m.role='assistant' AND tm.purpose='final'
 AND t.status IN ('running','complete')
 AND NOT EXISTS(SELECT 1 FROM forgotten_sessions f WHERE f.session_id=m.session_id)
)
BEGIN SELECT RAISE(ABORT,'citations require a final assistant message'); END;
CREATE TRIGGER IF NOT EXISTS message_citations_no_update BEFORE UPDATE ON message_citations
WHEN NOT (NEW.message_id=OLD.message_id AND OLD.redacted=0
          AND NEW.redacted=1 AND NEW.body IS NULL)
BEGIN SELECT RAISE(ABORT,'message citations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS message_citations_no_delete BEFORE DELETE ON message_citations
BEGIN SELECT RAISE(ABORT,'message citations are immutable'); END;
"""


def save(db, message_id, value):
    """Caller owns final-message transaction; malformed evidence rolls it back."""
    result = normalize(value)
    db.execute(
        "INSERT INTO message_citations(message_id,body) VALUES(?,?)",
        (message_id, json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
    )
    return result


def read(db, message_id):
    """No evidence escapes a forgotten or redacted source."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_citations'").fetchone():
        return []
    row = db.execute(
        "SELECT c.body FROM message_citations c JOIN messages m ON m.id=c.message_id "
        "WHERE c.message_id=? AND c.redacted=0 AND NOT EXISTS "
        "(SELECT 1 FROM forgotten_sessions f WHERE f.session_id=m.session_id)",
        (message_id,),
    ).fetchone()
    return normalize(json.loads(row[0])) if row else []


def redact(db, session_ids):
    """Offline forgetting hook, including excerpts/labels/hrefs; caller scrubs pages."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_citations'").fetchone():
        return
    for identity in session_ids:
        db.execute(
            "UPDATE message_citations SET body=NULL,redacted=1 WHERE redacted=0 "
            "AND message_id IN (SELECT id FROM messages WHERE session_id=?)",
            (identity,),
        )


def from_openrouter_annotations(value):
    """Map documented Chat Completion URL annotations; never enable retrieval.

    https://openrouter.ai/docs/guides/features/plugins/web-search#parsing-web-search-results
    IDs identify supplied annotation positions within this exact message.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Provider annotations must be a bounded list.")
    result = []
    for index, annotation in enumerate(value):
        if not isinstance(annotation, dict):
            raise ValueError("Invalid provider annotation.")
        if annotation.get("type") != "url_citation":
            continue
        supplied = annotation.get("url_citation")
        if not isinstance(supplied, dict):
            raise ValueError("Invalid URL citation annotation.")
        item = {
            "id": f"url-citation-{index + 1}",
            "label": supplied.get("title"),
            "href": supplied.get("url"),
        }
        if not isinstance(item["href"], str) or not item["href"]:
            raise ValueError("URL citation annotation needs a URL.")
        if "content" in supplied:
            item["excerpt"] = supplied["content"]
        result.append(item)
    return normalize(result)
