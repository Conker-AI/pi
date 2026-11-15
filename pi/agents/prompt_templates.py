"""Prompt templates (R-F8): a named list of fields the composer shows as a form and the wand can
fill from the owner's draft (`pi.agents.prompt_enhance`, `pi.agents.template_fill`).

The owner's own data, not chat data: forgetting a chat never touches a template, so nothing here
is filtered by `visibility`. Four starters are seeded once (`template_starters`); they are ordinary
rows afterwards, and a marker row keeps a deleted starter from coming back. At most MAX_TEMPLATES.
Changing a template changes only what the next form or enhance shows; it grants nothing, so the
owner session alone may write.

Design: context/design/prompt-templates.md in the Conker workspace.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Literal

from pydantic import Field, field_validator, model_validator

from pi.agents.template_starters import STARTERS
from pi.core.errors import ServiceError
from pi.core.models import Strict
from pi.core.untrusted_text import strip_invisible

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_templates (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, fields TEXT NOT NULL,
 starter INTEGER NOT NULL CHECK(starter IN (0,1)), revision INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_template_seed (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), seeded_at REAL NOT NULL
);
"""

MAX_TEMPLATES = 100
MAX_FIELDS = 12
MAX_OPTIONS = 20
NAME_LIMIT = 60
DESCRIPTION_LIMIT = 200
LABEL_LIMIT = 40
HINT_LIMIT = 200
OPTION_LIMIT = 60
TEMPLATE_ID = r"^(tpl_[0-9a-f]{32}|starter-[a-z0-9]+(-[a-z0-9]+)*)$"
NOT_THERE = "That template is not there."

FieldType = Literal["shortText", "longText", "list", "choice", "number", "date", "switch"]


class TemplateError(ServiceError):
    pass


def _line(value: str, what: str) -> str:
    cleaned = strip_invisible(value).strip()
    if not cleaned or any(ord(c) < 32 for c in cleaned):
        raise ValueError(f"{what} must be one nonempty line of text.")
    return cleaned


def _block(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    cleaned = strip_invisible(value).strip()
    if any(ord(c) < 32 and c != "\n" for c in cleaned):
        raise ValueError(f"{what} has a control character.")
    return cleaned or None


class TemplateField(Strict):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    label: str = Field(min_length=1, max_length=LABEL_LIMIT)
    type: FieldType
    required: bool = False
    example: str | None = Field(default=None, max_length=HINT_LIMIT)
    help: str | None = Field(default=None, max_length=HINT_LIMIT)
    options: list[str] | None = Field(default=None, min_length=1, max_length=MAX_OPTIONS)
    multiple: bool = False

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _line(value, "A label")

    @field_validator("example", "help")
    @classmethod
    def _hint(cls, value: str | None) -> str | None:
        return _block(value, "A hint")

    @field_validator("options")
    @classmethod
    def _options(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [_line(option, "An option") for option in value]
        if any(len(option) > OPTION_LIMIT for option in cleaned):
            raise ValueError(f"An option has at most {OPTION_LIMIT} characters.")
        if len({option.casefold() for option in cleaned}) != len(cleaned):
            raise ValueError("The options must differ from each other.")
        return cleaned

    @model_validator(mode="after")
    def _shape(self) -> TemplateField:
        if self.type == "choice" and not self.options:
            raise ValueError("A choice field needs its options.")
        if self.type != "choice" and (self.options is not None or self.multiple):
            raise ValueError("Only a choice field has options or allows several.")
        return self


class TemplateBody(Strict):
    name: str = Field(min_length=1, max_length=NAME_LIMIT)
    description: str = Field(default="", max_length=DESCRIPTION_LIMIT)
    fields: list[TemplateField] = Field(min_length=1, max_length=MAX_FIELDS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _line(value, "The name")

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return _block(value, "The description") or ""

    @model_validator(mode="after")
    def _distinct(self) -> TemplateBody:
        if len({field.id for field in self.fields}) != len(self.fields):
            raise ValueError("Each field needs its own id.")
        return self


class TemplateUpdate(TemplateBody):
    expected_revision: int = Field(ge=1, le=2147483646)


class TemplateDelete(Strict):
    # Given, it must match; left out, the delete applies to whatever revision is current.
    expected_revision: int | None = Field(default=None, ge=1, le=2147483646)


class Template(Strict):
    id: str
    name: str
    description: str
    fields: list[TemplateField]
    starter: bool
    revision: int
    createdAt: float
    updatedAt: float


class TemplatePage(Strict):
    items: list[Template]
    total: int


class TemplateReceipt(Strict):
    template: Template


class TemplateDeleted(Strict):
    id: str
    deleted: bool


def _stored(fields: list[TemplateField]) -> str:
    """What is saved: every field without its unset parts. `required` and `multiple` are always
    there; `example`, `help` and `options` only when set."""
    kept = [field.model_dump(exclude_none=True) for field in fields]
    return json.dumps(kept, ensure_ascii=False)


def _view(row) -> dict:
    return Template(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        fields=[TemplateField.model_validate(item) for item in json.loads(row["fields"])],
        starter=bool(row["starter"]),
        revision=row["revision"],
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
    ).model_dump(exclude_none=True)


def _row(db, template_id: str):
    row = None
    if re.fullmatch(TEMPLATE_ID, template_id):
        row = db.execute("SELECT * FROM prompt_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise TemplateError("template_not_found", NOT_THERE, 404)
    return row


def _check_room(db) -> None:
    if db.execute("SELECT COUNT(*) FROM prompt_templates").fetchone()[0] >= MAX_TEMPLATES:
        raise TemplateError(
            "too_many", f"There are already {MAX_TEMPLATES} templates; delete some first.", 409
        )


def initialize(db) -> None:
    """Install the tables and, once per database, the four starters."""
    db.executescript(SCHEMA)
    db.execute("BEGIN IMMEDIATE")
    try:
        marked = db.execute(
            "INSERT OR IGNORE INTO prompt_template_seed(singleton, seeded_at) VALUES (1, ?)",
            (time.time(),),
        ).rowcount
        if marked:
            now = time.time()
            for number, starter in enumerate(STARTERS):
                body = TemplateBody.model_validate(
                    {key: starter[key] for key in ("name", "description", "fields")}
                )
                # Listed in this order: the creation times differ by a thousandth of a second.
                stamp = now + number / 1000
                db.execute(
                    "INSERT INTO prompt_templates(id,name,description,fields,starter,revision,"
                    "created_at,updated_at) VALUES (?,?,?,?,1,1,?,?)",
                    (
                        starter["id"],
                        body.name,
                        body.description,
                        _stored(body.fields),
                        stamp,
                        stamp,
                    ),
                )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def listing(store) -> dict:
    """Every template: the starters first in their own order, then his, newest first."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM prompt_templates ORDER BY starter DESC, "
            "CASE starter WHEN 1 THEN created_at ELSE -created_at END, id"
        ).fetchall()
    return {"items": [_view(row) for row in rows], "total": len(rows)}


def get(store, template_id: str) -> dict:
    with store._connect() as db:
        return _view(_row(db, template_id))


def create(store, body: TemplateBody) -> dict:
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _check_room(db)
        now = time.time()
        template_id = "tpl_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO prompt_templates(id,name,description,fields,starter,revision,"
            "created_at,updated_at) VALUES (?,?,?,?,0,1,?,?)",
            (template_id, body.name, body.description, _stored(body.fields), now, now),
        )
        template = _view(_row(db, template_id))
        db.commit()
    return {"template": template}


def _same_revision(row, expected: int | None) -> None:
    if expected is not None and expected != row["revision"]:
        raise TemplateError(
            "revision_conflict",
            "This template changed; read it again.",
            409,
            revision=row["revision"],
        )


def update(store, template_id: str, body: TemplateUpdate) -> dict:
    """Replace the name, description and fields of one template."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _same_revision(_row(db, template_id), body.expected_revision)
        db.execute(
            "UPDATE prompt_templates SET name=?, description=?, fields=?, revision=revision+1, "
            "updated_at=? WHERE id=?",
            (body.name, body.description, _stored(body.fields), time.time(), template_id),
        )
        template = _view(_row(db, template_id))
        db.commit()
    return {"template": template}


def delete(store, template_id: str, body: TemplateDelete) -> dict:
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _same_revision(_row(db, template_id), body.expected_revision)
        db.execute("DELETE FROM prompt_templates WHERE id=?", (template_id,))
        db.commit()
    return {"id": template_id, "deleted": True}
