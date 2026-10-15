"""Owner-only metadata projection of Codex's isolated saved thread history."""

import re
import unicodedata
from typing import Annotated, Literal

import httpx
from pydantic import Field, field_validator, model_validator

from pi.native_codex_query import CURSOR, THREAD_ID, canonical_query, parse_query

from .native_sessions import NativeSessionsError, _json
from .provider_control import Strict

TIMEOUT = 30
CAPS = {"list": 2 * 1024 * 1024, "status": 65536}
ERROR_CODES = {
    "invalid_native_session_request": 422,
    "native_session_not_found": 404,
    "native_sessions_unavailable": 503,
}
ThreadId = Annotated[str, Field(pattern=rf"^{THREAD_ID}$", min_length=36, max_length=36)]
Cursor = Annotated[str, Field(pattern=rf"^{CURSOR}$", max_length=8192)]
SafeInt = Annotated[int, Field(ge=0, le=9007199254740991)]


class Thread(Strict):
    id: ThreadId
    name: str | None = Field(max_length=300)
    preview: str = Field(max_length=500)
    previewTruncated: bool
    cwd: str = Field(min_length=1, max_length=4096)
    createdAt: SafeInt
    updatedAt: SafeInt
    modelProvider: str = Field(min_length=1, max_length=200)
    source: Literal["cli", "vscode", "exec", "appServer", "subAgent", "custom", "unknown"]

    @field_validator("name", "preview", "cwd", "modelProvider")
    @classmethod
    def inert_text(cls, value):
        if value is not None and any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
        ):
            raise ValueError("Unsafe native metadata.")
        return value


class Evidence(Strict):
    schemaVersion: Literal[1]
    provider: Literal["codex"]
    scope: Literal["saved_threads"]
    runtimeVisibility: Literal["observer_only"]

    @model_validator(mode="before")
    @classmethod
    def exact_version(cls, value):
        if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int:
            raise ValueError("Invalid native metadata version.")
        return value


class Catalogue(Evidence):
    archived: bool
    results: list[Thread] = Field(max_length=50)
    nextCursor: Cursor | None

    @model_validator(mode="after")
    def distinct_threads(self):
        if len({row.id for row in self.results}) != len(self.results):
            raise ValueError("Duplicate native thread.")
        return self


class Status(Evidence):
    session: Thread


def request(transport, action, identity=None, query=b""):
    try:
        if action == "list" and identity is None:
            selected = parse_query(query)
            query = canonical_query(query)
        elif (
            action == "status"
            and isinstance(identity, str)
            and re.fullmatch(THREAD_ID, identity)
            and query == b""
        ):
            selected = None
        else:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise NativeSessionsError("invalid_native_session_request") from None
    if transport is None:
        raise NativeSessionsError()
    path = "/codex/sessions" + (f"/{identity}" if identity else "")
    url = httpx.URL("http://conker-host" + path).copy_with(query=query or None)
    try:
        with (
            httpx.Client(
                transport=transport, timeout=TIMEOUT, trust_env=False, follow_redirects=False
            ) as client,
            client.stream("GET", url) as response,
        ):
            if 300 <= response.status_code < 400:
                raise ValueError()
            raw = bytearray()
            cap = CAPS[action] if response.status_code == 200 else 4096
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(raw) + len(chunk) > cap:
                    raise ValueError()
                raw.extend(chunk)
            value = _json(raw)
            if response.status_code != 200:
                code = value.get("error") if isinstance(value, dict) else None
                if (
                    isinstance(value, dict)
                    and set(value) == {"error"}
                    and code in ERROR_CODES
                    and ERROR_CODES[code] == response.status_code
                ):
                    raise NativeSessionsError(code)
                raise ValueError()
            result = (Catalogue if action == "list" else Status).model_validate(value)
            if action == "list":
                if (
                    result.archived != selected["archived"]
                    or len(result.results) > selected["limit"]
                ):
                    raise ValueError()
                if result.nextCursor is not None and result.nextCursor == selected.get("cursor"):
                    raise ValueError()
            elif result.session.id != identity:
                raise ValueError()
            return result.model_dump()
    except (httpx.HTTPError, ValueError, UnicodeError, RecursionError, TypeError):
        raise NativeSessionsError() from None
