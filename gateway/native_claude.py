"""Owner-authenticated control for the unmodified Claude Code process."""

from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from .provider_control import ProviderControlError, Strict


class Operation(Strict):
    operation: Literal["login", "cancel", "logout"]
    billing: Literal["subscription", "api"] | None = None


class Status(Strict):
    available: bool
    connected: bool
    loginState: Literal["idle", "pending", "complete", "failed", "expired", "canceled"]
    authorizationUrl: str | None = Field(max_length=4096)
    credentialsIncluded: Literal[False]
    runtime: Literal["Claude Code"]
    models: list[str] = Field(max_length=100)


def request(transport, method, body=None):
    if transport is None:
        return dict(
            available=False,
            connected=False,
            loginState="idle",
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
