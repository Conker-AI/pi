"""Bounded gateway client for Conker's private host provider-control socket."""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ProviderId = Literal["openrouter", "openai", "anthropic"]
Revision = Annotated[str, Field(pattern=r"^credential_[0-9a-f]{32}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Stage(Strict):
    operation: Literal["stage"]
    provider: ProviderId
    secret: str = Field(min_length=8, max_length=4096, pattern=r"^[!-~]+$")
    activeRevision: Revision | None
    stagedRevision: Revision | None


class Action(Strict):
    operation: Literal["verify", "activate", "recover", "discard"]
    provider: ProviderId
    revision: Revision


class Revocation(Strict):
    operation: Literal["record-revoked"]
    provider: ProviderId
    revision: Revision
    issuerConfirmed: Literal[True]


class PaidPolicy(Strict):
    operation: Literal["paid-policy"]
    enabled: bool
    expectedAllowed: bool


class PaidRecovery(Strict):
    operation: Literal["recover-paid-policy"]


class ProviderState(Strict):
    id: ProviderId
    configured: bool
    activeRevision: Revision | None
    activeAt: str | None = Field(max_length=64)
    stagedRevision: Revision | None
    stagedAt: str | None = Field(max_length=64)
    verificationStatus: Literal["unverified", "verified", "rejected", "unavailable"] | None
    verificationBasis: str | None = Field(max_length=128)
    verifiedAt: str | None = Field(max_length=64)
    verificationStale: bool
    activationPending: bool
    revokedRevisions: list[Revision] = Field(max_length=100)
    secretIncluded: Literal[False]


class Status(Strict):
    schemaVersion: Literal[1]
    available: bool
    secretsIncluded: Literal[False]
    paidAllowed: bool | None
    policyRecoveryRequired: bool
    providers: list[ProviderState] = Field(max_length=3)


class ProviderControlError(ValueError):
    pass


def validate_operation(body: dict) -> dict:
    try:
        model = {
            "stage": Stage,
            "verify": Action,
            "activate": Action,
            "recover": Action,
            "discard": Action,
            "record-revoked": Revocation,
            "paid-policy": PaidPolicy,
            "recover-paid-policy": PaidRecovery,
        }.get(body.get("operation"))
        if model is None:
            raise ValueError()
        return model.model_validate(body).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        # Pydantic errors contain rejected input values; never propagate them.
        raise ProviderControlError("Invalid provider operation.") from None


def request(
    socket: str,
    method: str,
    body: dict | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    if not socket:
        if method == "GET":
            return {
                "schemaVersion": 1,
                "available": False,
                "secretsIncluded": False,
                "paidAllowed": None,
                "policyRecoveryRequired": False,
                "providers": [],
            }
        raise ProviderControlError("Host provider control is not configured.")
    try:
        with (
            httpx.Client(
                transport=transport or httpx.HTTPTransport(uds=socket),
                trust_env=False,
                follow_redirects=False,
                timeout=190,
            ) as client,
            client.stream(method, "http://conker-host/providers", json=body) as response,
        ):
            if response.status_code != 200:
                raise ProviderControlError(
                    "Host provider operation was not confirmed; refresh its status before retrying."
                )
            if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                raise ProviderControlError("Host provider status is unavailable.")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 65536:
                    raise ProviderControlError("Host provider status is too large.")
            result = Status.model_validate_json(content)
            if (
                not result.available
                or result.paidAllowed is None
                or {row.id for row in result.providers}
                != {
                    "openai",
                    "openrouter",
                    "anthropic",
                }
            ):
                raise ProviderControlError("Host provider status is incomplete.")
            return result.model_dump(mode="json")
    except (httpx.HTTPError, OSError, ValidationError):
        raise ProviderControlError(
            "Host provider control is unavailable; no operation was retried."
        ) from None
