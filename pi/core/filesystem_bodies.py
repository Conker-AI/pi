"""The directory-listing request body, shared by the Pi route and the owner gateway."""

from pydantic import Field, field_validator

from pi.core.models import Strict


class Read(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    root_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    path: str = ""
    limit: int = Field(default=200, ge=1, le=200)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        if not _relative(value):
            raise ValueError("Use a safe relative directory path.")
        return value


def _component(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value not in (".", "..")
        and not any(
            c in "/\\" or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value
        )
    )


def _relative(value):
    return (
        isinstance(value, str)
        and len(value) <= 4096
        and (value == "" or all(_component(c) for c in value.split("/")))
    )
