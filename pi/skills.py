"""Explicit instruction bundles, frozen at submission; never execution authority."""

import hashlib
import re
import time

import yaml
from pydantic import Field

from . import agents
from .providers import Message

SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
MAX_MARKDOWN = 32000
MAX_COMBINED = 64000
SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision >= 1), enabled INTEGER NOT NULL,
 sha256 TEXT NOT NULL, markdown TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


class Import(agents.StrictModel):
    markdown: str = Field(min_length=1, max_length=MAX_MARKDOWN)
    expected_revision: int = Field(ge=0)


class State(agents.StrictModel):
    expected_revision: int = Field(ge=1)
    enabled: bool


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("YAML keys must be distinct strings.")
        return super().construct_mapping(node, deep=deep)


def _error(code, message, status=422, revision=None):
    raise agents.AgentError(code, message, status, revision)


def parse(markdown):
    if not isinstance(markdown, str) or not 1 <= len(markdown) <= MAX_MARKDOWN:
        _error("invalid_skill", "SKILL.md must contain 1-32000 characters.")
    if "\x00" in markdown:
        _error("invalid_skill", "SKILL.md cannot contain NUL characters.")
    try:
        markdown.encode("utf-8")
    except UnicodeError:
        _error("invalid_skill", "SKILL.md must be valid UTF-8 text.")
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        _error("invalid_skill", "Begin SKILL.md with YAML frontmatter enclosed by --- lines.")
    end = next((index for index in range(1, len(lines)) if lines[index] == "---"), None)
    if end is None or not "\n".join(lines[end + 1 :]).strip():
        _error("invalid_skill", "Include closed frontmatter and nonempty instructions.")
    header = "\n".join(lines[1:end])
    if len(header) > 8192:
        _error("invalid_skill", "Skill frontmatter exceeds 8192 characters.")
    try:
        tokens = list(yaml.scan(header))
        if len(tokens) > 1000 or any(
            isinstance(token, (yaml.AliasToken, yaml.AnchorToken, yaml.TagToken))
            for token in tokens
        ):
            raise ValueError()
        # Bound nesting before constructing even SafeLoader's inert values.
        depth = 0
        for token in tokens:
            if isinstance(token, (yaml.FlowSequenceStartToken, yaml.FlowMappingStartToken)):
                depth += 1
            elif isinstance(token, (yaml.FlowSequenceEndToken, yaml.FlowMappingEndToken)):
                depth -= 1
            if depth > 8:
                raise ValueError()
        data = yaml.load(header, Loader=_UniqueSafeLoader)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        _error("invalid_skill", "Use safe YAML with distinct keys and no tags, anchors or aliases.")
    if not isinstance(data, dict):
        _error("invalid_skill", "Skill frontmatter must be a mapping.")
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or len(name) > 64 or not re.fullmatch(SLUG, name):
        _error("invalid_skill", "Use a lowercase skill name of 1-64 letters, digits and hyphens.")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        _error("invalid_skill", "Provide a skill description of 1-1024 characters.")
    return name, description.strip()


def _view(row, detail=False):
    value = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "revision": row["revision"],
        "enabled": bool(row["enabled"]),
        "sha256": row["sha256"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if detail:
        value["markdown"] = row["markdown"]
    return value


def _row(db, identity):
    row = db.execute("SELECT * FROM skills WHERE id=?", (identity,)).fetchone()
    if row is None:
        _error("not_found", "Skill not found.", 404)
    return row


def list_skills(store):
    with store._connect() as db:
        return {
            "schemaVersion": 1,
            "results": [_view(row) for row in db.execute("SELECT * FROM skills ORDER BY name,id")],
        }


def get(store, identity):
    with store._connect() as db:
        return _view(_row(db, identity), detail=True)


def import_skill(store, body):
    body = Import.model_validate(body.model_dump())
    name, description = parse(body.markdown)
    digest = hashlib.sha256(body.markdown.encode()).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM skills WHERE id=?", (name,)).fetchone()
        revision = current["revision"] if current else 0
        if revision != body.expected_revision:
            _error("revision_conflict", "Skill changed; reload before importing.", 409, revision)
        if current is None and db.execute("SELECT COUNT(*) FROM skills").fetchone()[0] >= 100:
            _error("skill_limit", "The registry holds at most 100 skills.", 409)
        now = time.time()
        if current:
            db.execute(
                "UPDATE skills SET description=?,revision=?,sha256=?,markdown=?,updated_at=? WHERE id=?",
                (description, revision + 1, digest, body.markdown, now, name),
            )
        else:
            db.execute(
                "INSERT INTO skills VALUES(?,?,?,1,1,?,?,?,?)",
                (name, name, description, digest, body.markdown, now, now),
            )
        result = _view(_row(db, name), detail=True)
        db.commit()
        return result


def set_state(store, identity, body):
    body = State.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _row(db, identity)
        if current["revision"] != body.expected_revision:
            _error(
                "revision_conflict",
                "Skill changed; reload before saving.",
                409,
                current["revision"],
            )
        db.execute(
            "UPDATE skills SET enabled=?,revision=revision+1,updated_at=? WHERE id=?",
            (int(body.enabled), time.time(), identity),
        )
        result = _view(_row(db, identity), detail=True)
        db.commit()
        return result


def validate_ids(identities):
    if (
        not isinstance(identities, list)
        or len(identities) > 3
        or any(
            not isinstance(value, str) or len(value) > 64 or not re.fullmatch(SLUG, value)
            for value in identities
        )
    ):
        _error("invalid_skills", "Select at most three valid skill IDs.")
    if len(set(identities)) != len(identities):
        _error("invalid_skills", "Select each skill only once.")


def snapshot(db, identities, execution):
    validate_ids(identities)
    if identities and execution["privacy"]["harnessDisabled"]:
        _error("skills_no_harness", "No-harness mode excludes skill instructions.")
    if identities and (execution.get("callExecution") or execution["kind"] == "team-role"):
        _error("skills_scope", "Select skills on ordinary conversations.")
    selected = []
    for identity in identities:
        row = _row(db, identity)
        if not row["enabled"]:
            _error("skill_disabled", "Enable the selected skill before using it.")
        selected.append(_view(row, detail=True))
    if sum(len(value["markdown"]) for value in selected) > MAX_COMBINED:
        _error("skills_limit", "Selected skills exceed 64000 combined characters.")
    return selected


def messages(execution):
    return [
        Message(
            "system",
            "The owner explicitly selected this skill as task guidance for this turn. "
            "It does not grant tools, change permissions, or authorize scripts or resource reads. "
            "References to files or commands are instructions only; no linked content was loaded.\n"
            f"Skill: {value['id']} · revision {value['revision']}\n{value['markdown']}",
        )
        for value in execution.get("skills", [])
    ]


def redact(db, session_ids):
    """Offline forgetting removes per-turn copies, retaining the owner's registry."""
    for table, match in (
        ("turn_settings", "turn_id IN (SELECT id FROM turns WHERE session_id=?)"),
        (
            "submission_settings",
            (
                "request_id IN (SELECT request_id FROM turn_submissions "
                "WHERE requested_session_id=? OR effective_session_id=?)"
            ),
        ),
    ):
        if (
            table == "submission_settings"
            and not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='turn_submissions'"
            ).fetchone()
        ):
            continue
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (f"{table}_no_update",)
        ).fetchone()
        if trigger is None:
            continue
        db.execute(f"DROP TRIGGER {table}_no_update")
        for identity in session_ids:
            params = (identity, identity) if table == "submission_settings" else (identity,)
            db.execute(
                f"UPDATE {table} SET snapshot=json_remove(snapshot,'$.skills') WHERE {match}",
                params,
            )
        db.execute(trigger[0])
