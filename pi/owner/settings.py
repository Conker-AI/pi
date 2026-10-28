"""The owner's settings document: one value per setting that has no other home yet.

Conker's settings registry (in the Conker repository) describes every setting once: its title, its
type, its default and where it lives. Settings with a home of their own (quiet hours in owner
preferences, models in the model configuration, ...) stay there. Every other setting is kept here,
keyed by its registry id, so the owner can choose it now and a feature reads it when it is built.

This document holds preferences, never authority. Nothing here lets Conker do more: what Conker
may do lives in ToolGate and in documents that ask for the owner's password. A feature that reads
a value here checks it again itself, since this store only knows the value's shape, not its meaning.
Saves are revision-checked: a stale screen is refused instead of overwriting a newer change.
"""

from __future__ import annotations

import json
import re

from pydantic import Field

from pi.core.models import Strict

SETTING_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,6}$")
MAX_SETTINGS = 2000
MAX_VALUE_BYTES = 8 * 1024
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_DEPTH = 4
SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL CHECK(revision>=0),
    vals TEXT NOT NULL
);
"""


class RevisionConflict(Exception):
    def __init__(self, current_revision):
        self.current_revision = current_revision
        super().__init__("Settings changed elsewhere. Reload before saving.")


class Update(Strict):
    expected_revision: int = Field(ge=0)
    values: dict


def initialize(db):
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO owner_settings VALUES (1,0,'{}')")


def _depth(value, level=0) -> int:
    if isinstance(value, dict):
        return max([level, *(_depth(item, level + 1) for item in value.values())])
    if isinstance(value, list):
        return max([level, *(_depth(item, level + 1) for item in value)])
    return level


def check(values: dict) -> dict:
    """Shape only: known-looking ids, JSON values of bounded size. Meaning is the reader's job."""
    if len(values) > MAX_SETTINGS:
        raise ValueError(f"Keep at most {MAX_SETTINGS} settings.")
    for key, value in values.items():
        if not isinstance(key, str) or not SETTING_ID.fullmatch(key):
            raise ValueError(f"{key!r} is not a setting id.")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be plain JSON.") from exc
        if len(encoded.encode()) > MAX_VALUE_BYTES:
            raise ValueError(f"{key} is too large.")
        if _depth(value) > MAX_DEPTH:
            raise ValueError(f"{key} is nested too deeply.")
    if len(json.dumps(values, ensure_ascii=False).encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError("The settings document is too large.")
    return values


def load(store) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT revision, vals FROM owner_settings WHERE singleton=1").fetchone()
    return {"schemaVersion": 1, "revision": row["revision"], "values": json.loads(row["vals"])}


def save(store, request: Update) -> dict:
    """Replace the document; a value left out goes back to its default."""
    values = check(dict(request.values))
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        revision = db.execute("SELECT revision FROM owner_settings WHERE singleton=1").fetchone()[0]
        if revision != request.expected_revision:
            raise RevisionConflict(revision)
        db.execute(
            "UPDATE owner_settings SET revision=?, vals=? WHERE singleton=1",
            (revision + 1, json.dumps(values, ensure_ascii=False, sort_keys=True)),
        )
        db.commit()
    return {"schemaVersion": 1, "revision": revision + 1, "values": values}


def value(store, setting_id: str, default=None):
    """One stored value for a feature that reads it; the default when the owner never set it."""
    return load(store)["values"].get(setting_id, default)
