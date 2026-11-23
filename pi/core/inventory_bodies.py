"""The system-inventory request body, shared by the Pi route and the owner gateway."""

from pydantic import Field

from pi.core.models import Strict


class Read(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    limit: int = Field(default=100, ge=1, le=200)
