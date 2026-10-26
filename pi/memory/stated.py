"""The owner adds a memory in their own words: stored word for word, once per request id.

The same MemoryGate route stores what the owner approves from Conker's requests, so a memory added
by hand and one Conker asked for are kept the same way.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from pi.core.errors import ServiceError
from pi.core.models import Strict


class Remember(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def stated(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Write the memory in plain text.")
        return value


class RememberError(ServiceError):
    pass


def remember(client, body: Remember) -> dict:
    """Retrying with the same request id is safe: MemoryGate stores it at most once."""
    if client is None:
        raise RememberError(
            "memory_unavailable", "Memory isn't connected, so nothing was saved.", 503
        )
    try:
        return client.remember(body.request_id, body.text)
    except Exception:
        raise RememberError(
            "memory_unavailable", "Memory didn't answer, so nothing was saved. Try again.", 503
        ) from None
