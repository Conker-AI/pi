"""Strict owner-only provider login/catalogue operations over the host socket."""

from typing import Literal

import httpx
from pydantic import Field, model_validator

from .provider_control import ProviderControlError, ProviderId, Revision, Strict


class Login(Strict):
    operation: Literal["login"]
    provider: Literal["openrouter"]
    activeRevision: Revision | None
    stagedRevision: Revision | None


class Cancel(Strict):
    operation: Literal["cancel"]
    provider: Literal["openrouter"]
    loginId: str = Field(pattern=r"^login_[0-9a-f]{32}$")


class Complete(Cancel):
    operation: Literal["complete"]
    code: str = Field(min_length=8, max_length=2048, pattern=r"^[A-Za-z0-9_.-]+$")


class Discover(Strict):
    operation: Literal["models"]
    provider: ProviderId
    revision: Revision


class LoginStatus(Strict):
    kind: Literal["login"]
    available: bool
    provider: Literal["openrouter"]
    loginId: str | None = Field(pattern=r"^login_[0-9a-f]{32}$")
    loginState: Literal["idle", "pending", "complete", "failed", "expired", "canceled"]
    credentialsIncluded: Literal[False]
    authorizationUrl: str | None = Field(
        default=None,
        pattern=r"^https://openrouter\.ai/auth\?code_challenge=[A-Za-z0-9_-]{43}&code_challenge_method=S256&key_label=Conker$",
    )

    @model_validator(mode="after")
    def coherent(self):
        if (self.loginState == "pending") != bool(self.loginId) or (
            self.authorizationUrl and not self.available
        ):
            raise ValueError("Incoherent provider login state.")
        return self


class Model(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:+-]{0,299}$")
    name: str = Field(min_length=1, max_length=160)


class Catalogue(Strict):
    kind: Literal["catalogue"]
    provider: ProviderId
    revision: Revision
    models: list[Model] = Field(max_length=1000)
    catalogueComplete: bool
    credentialsIncluded: Literal[False]


def validate_operation(body):
    try:
        model = {"login": Login, "cancel": Cancel, "complete": Complete, "models": Discover}.get(body.get("operation"))
        if model is None:
            raise ValueError()
        return model.model_validate(body).model_dump(mode="json")
    except (ValueError, TypeError):
        raise ProviderControlError("Invalid provider setup operation.") from None


def request(socket, method, body=None, *, transport=None):
    if not socket:
        if method == "GET":
            return LoginStatus(kind="login", available=False, provider="openrouter", loginId=None,
                               loginState="idle", credentialsIncluded=False).model_dump(mode="json")
        raise ProviderControlError("Host provider control is not configured.")
    try:
        with (
            httpx.Client(transport=transport or httpx.HTTPTransport(uds=socket),
                         timeout=70, trust_env=False, follow_redirects=False) as client,
            client.stream(method, "http://conker-host/provider-onboarding", json=body) as response,
        ):
            if response.status_code != 200 or response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                raise ValueError()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 524288:
                    raise ValueError()
            if method == "POST" and body and body["operation"] == "models":
                value = Catalogue.model_validate_json(content)
                if value.provider != body["provider"] or value.revision != body["revision"]:
                    raise ValueError()
            else:
                value = LoginStatus.model_validate_json(content)
                if value.authorizationUrl and (method != "POST" or not body or body["operation"] != "login"):
                    raise ValueError()
            return value.model_dump(mode="json")
    except (httpx.HTTPError, OSError, ValueError):
        raise ProviderControlError("Provider setup was not confirmed. Refresh status; no request was retried.") from None
