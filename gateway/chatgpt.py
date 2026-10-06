"""Owner-only subscription sign-in over a fixed private host socket."""

from typing import Literal

import httpx
from pydantic import Field, ValidationError

from .provider_control import ProviderControlError, Strict


class Model(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
    name: str = Field(min_length=1, max_length=160)


class DeviceCode(Strict):
    verificationUrl: Literal["https://auth.openai.com/codex/device"]
    userCode: str = Field(pattern=r"^[A-Z0-9-]{4,32}$")


class Status(Strict):
    available: bool
    connected: bool
    connectionId: str | None = Field(pattern=r"^connection_[0-9a-f]{32}$")
    plan: str | None = Field(max_length=80)
    loginId: str | None = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    loginState: Literal["idle", "pending", "complete", "failed", "expired", "canceled"]
    problem: (
        Literal["runtime_unavailable", "runtime_version_mismatch", "provider_operation_failed"]
        | None
    )
    models: list[Model] = Field(max_length=100)
    catalogueComplete: bool
    credentialsIncluded: Literal[False]
    deviceCode: DeviceCode | None = None


class Simple(Strict):
    operation: Literal["login", "models"]


class Cancel(Strict):
    operation: Literal["cancel"]
    loginId: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class Logout(Strict):
    operation: Literal["logout"]
    connectionId: str = Field(pattern=r"^connection_[0-9a-f]{32}$")


def validate_operation(body):
    try:
        model = {"login": Simple, "models": Simple, "cancel": Cancel, "logout": Logout}.get(
            body.get("operation")
        )
        if model is None:
            raise ValueError()
        return model.model_validate(body).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        raise ProviderControlError("Invalid subscription operation.") from None


def request(socket, method, body=None, *, transport=None):
    if not socket:
        if method == "GET":
            return Status(
                available=False,
                connected=False,
                connectionId=None,
                plan=None,
                loginId=None,
                loginState="idle",
                problem="runtime_unavailable",
                models=[],
                catalogueComplete=False,
                credentialsIncluded=False,
            ).model_dump(mode="json")
        raise ProviderControlError("Host provider control is not configured.")
    try:
        with (
            httpx.Client(
                transport=transport or httpx.HTTPTransport(uds=socket),
                timeout=70,
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream(method, "http://conker-host/chatgpt", json=body) as response,
        ):
            if response.status_code != 200:
                raise ProviderControlError(
                    "ChatGPT operation was not confirmed. Refresh status before retrying."
                )
            if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                raise ValueError()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 65536:
                    raise ValueError()
            status = Status.model_validate_json(content)
            if status.connected != bool(status.connectionId) or (
                status.deviceCode is not None
                and (method != "POST" or (body or {}).get("operation") != "login")
            ):
                raise ValueError()
            return status.model_dump(mode="json")
    except (httpx.HTTPError, OSError, ValueError):
        raise ProviderControlError(
            "ChatGPT control is unavailable. No operation was retried."
        ) from None
