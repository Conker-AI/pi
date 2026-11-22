"""Request bodies of the project routes: fields, link references and the revision-checked updates."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from pi.core.models import Strict

SessionId = Annotated[str, StringConstraints(pattern=r"^ses_[0-9a-f]{16}$")]
TaskId = Annotated[str, StringConstraints(pattern=r"^tsk_[0-9a-f]{32}$")]
FileId = Annotated[str, StringConstraints(pattern=r"^attachment_[0-9a-f]{32}$")]


class Fields(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=2000)
    instructions: str = Field(max_length=16000)

    @field_validator("name", "description")
    @classmethod
    def strip(cls, value, info):
        value = value.strip()
        if info.field_name == "name" and not value:
            raise ValueError("Provide a project name.")
        return value


class Conversation(Strict):
    kind: Literal["conversation"]
    sessionId: SessionId


class Task(Strict):
    kind: Literal["task"]
    taskId: TaskId


class File(Strict):
    kind: Literal["file"]
    sessionId: SessionId
    fileId: FileId


Reference = Annotated[Conversation | Task | File, Field(discriminator="kind")]


class Revision(Strict):
    expected_revision: int = Field(ge=1)


class Update(Revision):
    fields: Fields


class Archive(Revision):
    archived: bool


class Link(Revision):
    reference: Reference
