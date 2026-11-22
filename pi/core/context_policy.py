"""The shape of an owner's context policy, shared by context controls and calls."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from pi.core.models import Strict

PolicyName = Literal["keep-exact", "allow-summary", "retrieve", "exclude"]


class Budget(Strict):
    contextWindowTokens: int = Field(ge=2, le=10000000)
    outputReserveTokens: int = Field(ge=1)
    otherInputTokens: int = Field(ge=0)

    @model_validator(mode="after")
    def reserve(self):
        if self.outputReserveTokens >= self.contextWindowTokens:
            raise ValueError("Output reserve must be smaller than the context window.")
        return self


class Policy(Strict):
    sessionInstructions: str = Field(max_length=16000)
    messagePolicies: dict[str, PolicyName]
    budget: Budget

    @field_validator("messagePolicies")
    @classmethod
    def selectors(cls, value):
        if len(value) > 10000 or any(not key.strip() or len(key) > 200 for key in value):
            raise ValueError("Use bounded message identities.")
        return value
