"""Owner-authenticated control for the unmodified Claude Code process."""

from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from .provider_control import ProviderControlError, Strict


class Operation(Strict):
    operation: Literal["login", "cancel", "logout"]
    billing: Literal["subscription", "api"] | None = None
    loginId: str | None = Field(default=None, pattern=r"^login_[0-9a-f]{32}$")

    @model_validator(mode="after")
    def exact_operation(self):
        if self.operation == "cancel":
            if self.model_fields_set != {"operation", "loginId"} or self.loginId is None:
                raise ValueError("Cancellation requires the exact sign-in identity.")
        elif self.operation == "login":
            if not self.model_fields_set <= {"operation", "billing"}:
                raise ValueError("Invalid native authentication operation.")
        elif self.model_fields_set != {"operation"}:
            raise ValueError("Invalid native authentication operation.")
        return self


class Status(Strict):
    available: bool
    connected: bool
    loginState: Literal["idle", "pending", "complete", "failed", "expired", "canceled"]
    loginId: str | None = Field(pattern=r"^login_[0-9a-f]{32}$")
    authorizationUrl: str | None = Field(max_length=4096)
    credentialsIncluded: Literal[False]
    runtime: Literal["Claude Code"]
    models: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def pending_identity(self):
        if (self.loginState == "pending") != (self.loginId is not None):
            raise ValueError("Native sign-in status has no matching identity.")
        return self


def request(transport, method, body=None):
    if transport is None:
        return dict(
            available=False,
            connected=False,
            loginState="idle",
            loginId=None,
            authorizationUrl=None,
            credentialsIncluded=False,
            runtime="Claude Code",
            models=[],
        )
    try:
        with httpx.Client(
            transport=transport, timeout=30, trust_env=False, follow_redirects=False
        ) as client:
            response = client.request(method, "http://conker-host/claude", json=body)
            response.raise_for_status()
            if len(response.content) > 65536:
                raise ValueError()
            result = Status.model_validate_json(response.content)
            if result.authorizationUrl:
                url = urlsplit(result.authorizationUrl)
                if (
                    url.scheme != "https"
                    or url.hostname
                    not in {
                        "claude.ai",
                        "claude.com",
                        "console.anthropic.com",
                        "platform.claude.com",
                    }
                    or url.username
                    or url.password
                ):
                    raise ValueError()
            return result.model_dump()
    except (httpx.HTTPError, ValueError):
        raise ProviderControlError(
            "Native Claude Code status is unavailable. Refresh before retrying."
        ) from None
