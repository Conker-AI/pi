"""Shared request-model pieces: the one strict request model (exact types, no unknown fields)
and the reference-ID check."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def references(values):
    if any(not value.strip() or value != value.strip() or len(value) > 200 for value in values):
        raise ValueError("Use nonempty reference IDs of at most 200 characters.")
    if len(set(values)) != len(values):
        raise ValueError("Reference IDs must be unique.")
    return values


def described(model: type[BaseModel]) -> dict[int | str, dict[str, Any]]:
    """A route's `responses=` naming `model` as its 200 reply in OpenAPI (D-01). FastAPI only
    describes it: the reply is not re-validated or filtered, so it stays what the handler sends."""
    return {200: {"model": model}}
