"""Bounded inert attachment bytes and immutable session/message provenance."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from . import attachment_passages, document_text, pdf_text

MAX_FILES = 5
MAX_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_SESSION_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARACTERS = 200_000


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    type: str = Field(default="", max_length=255)
    lastModified: float = Field(default=0, ge=0, allow_inf_nan=False)

    @field_validator("name")
    @classmethod
    def filename(cls, value):
        if (
            value in (".", "..")
            or any(c in value for c in ("/", "\\", ":"))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Use a filename without paths or control characters.")
        return value

    @field_validator("type")
    @classmethod
    def mime(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid declared media type.")
        return value


class Privacy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    memoryDisabled: bool
    harnessDisabled: bool
    incognito: bool = False


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


class AttachmentError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _session(db, identity, resolve, *, active=False):
    row = db.execute("SELECT status FROM sessions WHERE id=?", (identity,)).fetchone()
    if (
        not row
        or row[0] == "forgotten"
        or db.execute("SELECT 1 FROM forgotten_sessions WHERE session_id=?", (identity,)).fetchone()
    ):
        raise AttachmentError("source_unavailable", "Attachment session unavailable.", 404)
    if active and row[0] != "open":
        raise AttachmentError("source_archived", "Upload requires an active session.")
    try:
        privacy = Privacy.model_validate(resolve(db, identity)) if resolve else None
    except Exception:
        privacy = None
    if privacy is None:
        raise AttachmentError("privacy_unknown", "Attachment privacy is unavailable.")
    return privacy.model_dump(), row[0] != "open"


def _row(db, session_id, identity):
    row = db.execute(
        "SELECT * FROM attachments WHERE id=? AND session_id=?", (identity, session_id)
    ).fetchone()
    if row is None:
        raise AttachmentError("not_found", "Attachment not found in this session.", 404)
    return row


def _view(db, row, resolve):
    privacy, archived, availability = None, False, "available"
    try:
        privacy, archived = _session(db, row["session_id"], resolve)
        if row["purged"]:
            raise AttachmentError(
                "removed" if row["reason"] == "removed" else "source_unavailable",
                "Attachment content was removed.",
            )
        original = Privacy.model_validate_json(row["privacy"]).model_dump()
        privacy = {key: privacy[key] or original[key] for key in privacy}
    except (AttachmentError, ValueError) as exc:
        availability = exc.detail["code"] if isinstance(exc, AttachmentError) else "privacy_unknown"
        privacy = None
    readable = availability == "available"
    return {
        "id": row["id"],
        "sessionId": row["session_id"],
        "name": row["name"] if readable else "Unavailable attachment",
        "size": row["size"],
        "type": row["media_type"] if readable else "",
        "lastModified": row["last_modified"],
        "createdAt": row["created_at"],
        "sha256": row["sha256"] if readable else None,
        "availability": "source-archived" if readable and archived else availability,
        "privacy": privacy,
        "privateOrigin": any(privacy.values()) if privacy else None,
        "processing": {
            "status": row["processing"] if readable else "unavailable",
            "reason": row["reason"] if readable else availability,
        },
        "execution": "not-wired",
    }


def _extract(raw, media_type):
    media_type = media_type.lower().strip()
    if media_type == "application/pdf":
        try:
            text = pdf_text.extract(raw, MAX_TEXT_CHARACTERS)
        except pdf_text.PDFError as error:
            return None, "unsupported", str(error)
        return text, "extracted", "PDF text layer with page labels; images, layout and OCR are not extracted."
    if media_type == document_text.DOCX:
        try:
            text = document_text.docx(raw, MAX_TEXT_CHARACTERS)
        except Exception:
            return None, "unsupported", "DOCX body text could not be safely extracted."
        if any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in text):
            return None, "unsupported", "Unsupported document control characters."
        return (
            text,
            "extracted",
            "DOCX body text only; images, layout, headers and footnotes are not extracted.",
        )
    if media_type not in ("text/plain", "text/markdown", "text/csv", "application/json"):
        return (
            None,
            "unsupported",
            "Supported text extraction: plain text, Markdown, CSV, JSON, DOCX body text and PDF text layers.",
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "unsupported", "Plaintext must use valid UTF-8 encoding."
    if len(text) > MAX_TEXT_CHARACTERS:
        return None, "unsupported", "Plaintext exceeds the extraction character limit."
    if any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in text):
        return None, "unsupported", "Binary control characters are not plaintext."
    return text, "extracted", "UTF-8 plaintext; inert, untrusted source content."


def upload(store, session_id, metadata: Metadata, raw: bytes, resolve=None):
    metadata = Metadata.model_validate(metadata.model_dump())
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise AttachmentError("file_limit", "Attachment must be at most 10 MiB.", 413)
    text, processing, reason = _extract(raw, metadata.type)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        privacy, _ = _session(db, session_id, resolve, active=True)
        used = db.execute(
            "SELECT COALESCE(SUM(size),0),COUNT(*) FROM attachments "
            "WHERE session_id=? AND purged=0",
            (session_id,),
        ).fetchone()
        if used[0] + len(raw) > MAX_SESSION_BYTES or used[1] >= 100:
            raise AttachmentError(
                "session_limit",
                "Session attachment storage is limited to 100 files and 100 MiB.",
                413,
            )
        identity = "attachment_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                identity,
                session_id,
                metadata.name,
                metadata.type,
                len(raw),
                metadata.lastModified,
                time.time(),
                raw,
                hashlib.sha256(raw).hexdigest(),
                text,
                processing,
                reason,
                json.dumps(privacy),
            ),
        )
        result = _view(db, _row(db, session_id, identity), resolve)
        db.commit()
        return result


def get(store, session_id, identity, resolve=None):
    with store._connect() as db:
        db.execute("BEGIN")
        return _view(db, _row(db, session_id, identity), resolve)


def listing(store, session_id, resolve=None):
    with store._connect() as db:
        db.execute("BEGIN")
        _session(db, session_id, resolve)
        return [
            _view(db, row, resolve)
            for row in db.execute(
                "SELECT * FROM attachments WHERE session_id=? ORDER BY created_at,id", (session_id,)
            )
        ]


def _verified(db, session_id, identity, resolve):
    row = _row(db, session_id, identity)
    view = _view(db, row, resolve)
    if view["availability"] not in ("available", "source-archived"):
        raise AttachmentError(
            "source_unavailable", "Attachment privacy or availability blocks content."
        )
    raw = bytes(row["content"])
    if len(raw) != row["size"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise AttachmentError("integrity_failure", "Attachment integrity check failed.")
    return row, view, raw


def download(store, session_id, identity, resolve=None):
    with store._connect() as db:
        db.execute("BEGIN")
        _, view, raw = _verified(db, session_id, identity, resolve)
        return view, raw


def extract(store, session_id, identity, resolve=None):
    with store._connect() as db:
        db.execute("BEGIN")
        row, view, raw = _verified(db, session_id, identity, resolve)
        text, status, _ = _extract(raw, row["media_type"])
        if status != "extracted":
            raise AttachmentError("processing_unsupported", row["reason"], 415)
        if text != row["extracted_text"]:
            raise AttachmentError("integrity_failure", "Extracted content integrity check failed.")
        return {
            "attachment": view,
            "text": text,
            "passages": attachment_passages.split(identity, text),
            "trust": "untrusted-source",
            "execution": "not-wired",
        }


def passage(store, session_id, identity, index, resolve=None):
    if type(index) is not int or index < 0:
        raise AttachmentError("invalid_passage", "Choose a non-negative passage index.", 422)
    value = extract(store, session_id, identity, resolve)
    if index >= len(value["passages"]):
        raise AttachmentError("not_found", "Passage is unavailable.", 404)
    return {
        "attachmentId": identity,
        "name": value["attachment"]["name"],
        "passage": value["passages"][index],
        "trust": "untrusted-source",
        "offsetUnit": "unicode-codepoints",
        "source": "extracted-text",
    }


def bind(db, session_id, message_id, attachment_ids):
    """Call inside message creation transaction, never as a post-hoc client mutation."""
    if not db.in_transaction:
        raise ValueError("Message attachment binding requires a transaction.")
    if (
        not isinstance(attachment_ids, list)
        or len(attachment_ids) > MAX_FILES
        or any(not isinstance(i, str) or not i or len(i) > 100 for i in attachment_ids)
        or len(set(attachment_ids)) != len(attachment_ids)
    ):
        raise AttachmentError("invalid_bindings", "Bind at most five distinct attachment IDs.", 422)
    message = db.execute(
        "SELECT session_id,role FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    if not message or message["session_id"] != session_id or message["role"] != "user":
        raise AttachmentError(
            "invalid_message", "Attachments require the originating user message."
        )
    if db.execute(
        "SELECT 1 FROM message_attachment_sets WHERE message_id=?", (message_id,)
    ).fetchone():
        raise AttachmentError("immutable_bindings", "Message attachment bindings are immutable.")
    rows = [_row(db, session_id, identity) for identity in attachment_ids]
    if any(row["purged"] for row in rows) or sum(row["size"] for row in rows) > MAX_MESSAGE_BYTES:
        raise AttachmentError(
            "message_limit", "Available attachments must total at most 25 MiB.", 413
        )
    db.execute(
        "INSERT INTO message_attachment_sets VALUES(?,?)", (message_id, json.dumps(attachment_ids))
    )
    db.executemany(
        "INSERT INTO message_attachments VALUES(?,?,?)",
        [(message_id, index, identity) for index, identity in enumerate(attachment_ids)],
    )


def message_views(db, message_id, resolve=None):
    return [
        _view(db, row, resolve)
        for row in db.execute(
            "SELECT a.* FROM attachments a JOIN message_attachments m ON m.attachment_id=a.id "
            "WHERE m.message_id=? ORDER BY m.position",
            (message_id,),
        )
    ]


def project_source(db, session_id, identity, resolve=None):
    """Metadata only; never grant context/model access or copy plaintext into projects."""
    try:
        view = _view(db, _row(db, session_id, identity), resolve)
    except AttachmentError:
        return None
    if view["availability"] not in ("available", "source-archived"):
        return None
    return {
        "originSessionId": session_id,
        "label": view["name"],
        "archived": view["availability"] == "source-archived",
        "privacy": view["privacy"],
    }


def remove(store, session_id, identity, resolve=None):
    """Soft purge unused upload; message/reservation provenance cannot be detached."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _session(db, session_id, resolve)
        row = _row(db, session_id, identity)
        prior = db.execute(
            "SELECT * FROM attachment_removals WHERE attachment_id=?", (identity,)
        ).fetchone()
        if prior:
            return dict(prior)
        if row["purged"]:
            raise AttachmentError("source_unavailable", "Attachment is unavailable.")
        if (
            db.execute(
                "SELECT 1 FROM message_attachments WHERE attachment_id=?", (identity,)
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM attachment_reservations WHERE attachment_id=?", (identity,)
            ).fetchone()
        ):
            raise AttachmentError(
                "attachment_in_use", "Bound or reserved attachments require session forgetting."
            )
        receipt = {
            "id": "attachment_removal_" + uuid.uuid4().hex,
            "attachment_id": identity,
            "removed_at": time.time(),
            "reason": "owner-removed",
        }
        db.execute("INSERT INTO attachment_removals VALUES(?,?,?,?)", tuple(receipt.values()))
        db.execute(
            "UPDATE attachments SET purged=1,name='',media_type='',content=NULL,sha256=NULL,"
            "extracted_text=NULL,privacy=NULL,processing='unavailable',reason='removed' "
            "WHERE id=?",
            (identity,),
        )
        db.commit()
        return receipt


def redact(db, session_ids):
    """Offline forgetting hook; caller owns atomicity and physical page cleanup."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='attachments'").fetchone():
        return
    for identity in session_ids:
        db.execute(
            "UPDATE attachments SET purged=1,name='',media_type='',content=NULL,sha256=NULL,"
            "extracted_text=NULL,privacy=NULL,processing='unavailable',reason='forgotten' "
            "WHERE session_id=? AND purged=0",
            (identity,),
        )
