"""The one strict request model: exact types, no unknown fields."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def described(model: type[BaseModel]) -> dict[int | str, dict[str, Any]]:
    """A route's `responses=` naming `model` as its 200 reply in OpenAPI (D-01). FastAPI only
    describes it: the reply is not re-validated or filtered, so it stays what the handler sends."""
    return {200: {"model": model}}
